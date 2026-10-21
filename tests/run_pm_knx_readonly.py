"""将真实 KNX 项目清单和原始客户聊天交给生产 PM，只观察一轮回复。"""

# ruff: noqa: E402, T201
import asyncio
import hashlib
import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from langchain.agents.middleware import AgentMiddleware
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

load_dotenv(ROOT / '.env')
# 仅本测试进程关闭 Worker，不修改真实部署配置。
os.environ['ACP_SUBAGENTS_JSON'] = '[]'
os.environ['ACP_WORKERS_JSON'] = '[]'
os.environ['HTTP_PROXY'] = 'http://127.0.0.1:7897'
os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:7897'
os.environ['NO_PROXY'] = 'localhost,127.0.0.1,::1'

from src.middlewares.memory.pm_agent_memory import PMMemory
from src.middlewares.memory.pm_agent_memory.files import memory_files
from src.middlewares.project_state import ProjectSnapshot, ProjectStore
from src.models.llm import get_default_model

PROJECT_ROOT = Path('C:/Users/User/wt/peoject/knx项目')
OUTPUT = ROOT / 'docs/progress/evidence/pm-knx-readonly-20261020.json'
REPLY = ROOT / 'docs/progress/evidence/pm-knx-readonly-20261020.md'
DOCUMENT = '/memories/topics/KNX_PROJECT_INPUT.md'
ALLOWED = {'project_context', 'project_read', 'memory_search', 'pm_read_memory', 'pm_read_bookmark'}

CHAT = '''春天@微信@微信联系人 10/20 12:04:27
昨天我们开会有理事提出能不能把网站上的案例展示也同步到小程序上，在小程序“学习天地”后面增加一个板块叫“应用案例”或“案例展示”，数据共同网站的数据，你看能做到吗？

张栋(Andy) 10/20 13:10:21
先把这版小程序发布出来吧

春天@微信@微信联系人 10/20 13:32:57
这个复杂吗？如果不复杂就加一下吧，现在小程序看着东西不是太多，我们也是想让小程序的内容能丰富一点，这样访问用户看的价值更高一些，能多吸引一些流量；如果添加复杂，那我们也可以先发布，后续再增加

春天@微信@微信联系人 10/20 13:34:25
前台页面可以做成和“学习天地”一样，显示两个图片，点更多进入列表

娜娜(项目经理) 10/20 13:34:57
建议您先发布，等稳定使用这一期了，咱们再增加，还是需要点工作量的

春天@微信@微信联系人 10/20 13:44:41
为了解决后期数据增加查询的困难，我们再加这两个选项吧

春天@微信@微信联系人 10/20 13:44:42
image

春天@微信@微信联系人 10/20 13:44:43
image

春天@微信@微信联系人 10/20 13:47:26
工程师等级分：初级、高级、导师
证书类型分：设计师证书，其他的后面有了再添加

春天@微信@微信联系人 10/20 13:48:40
拜托辛苦了🙏

春天@微信@微信联系人 10/20 13:56:02
咱们小程序体验版是否已经公开上线了？我可以发给我们的会员了吗？登录二维码能再发我一遍吗

娜娜(项目经理) 10/20 13:57:12
@春天 咱们昨天的正式版本已经发了，就是频繁的改动，一直会审核

娜娜(项目经理) 10/20 13:57:28
现在的最新版本是昨天改动好的

娜娜(项目经理) 10/20 13:58:21
今天上午改的去发布审核了还没有通过

春天@微信@微信联系人 10/20 13:59:34
哦，明白，今天提的两个问题也可以留到测试期间修改，不急这几天'''


async def main():
    evidence = {'status': 'running', 'entrypoint': 'production pm_agent factory',
                'project_root': str(PROJECT_ROOT), 'store': 'isolated InMemoryStore',
                'acp_enabled': False, 'project_writes_enabled': False,
                'tool_calls': [], 'model_responses': [], 'background_run_attempts': 0}
    resumed_messages = None
    if '--resume' in sys.argv:
        evidence = json.loads(OUTPUT.read_text(encoding='utf-8'))
        if evidence.get('error', {}).get('type') != 'GraphRecursionError':
            raise RuntimeError('仅允许从已记录的图步数上限恢复本轮')
        evidence.setdefault('execution_limits', []).append(evidence.pop('error'))
        evidence['status'] = 'resuming'
        # 还原已完成的工具返回和最后未执行的调用，不重新请求已有模型回复。
        resumed_messages = [HumanMessage(content=evidence['input'])]
        tool_index = 0
        for response in evidence['model_responses']:
            resumed_messages.append(AIMessage(content=response['content'], tool_calls=response['tool_calls']))
            for call in response['tool_calls']:
                if tool_index >= len(evidence['tool_calls']):
                    continue
                row = evidence['tool_calls'][tool_index]
                if row['name'] != call['name'] or row['args'] != call['args']:
                    raise RuntimeError('历史工具记录无法无损关联，停止恢复')
                resumed_messages.append(ToolMessage(content=row['result'], name=call['name'], tool_call_id=call['id']))
                tool_index += 1
        evidence['resume_scope'] = 'seed official checkpoint at last model output, then execute pending tools; no replay of completed model calls'

    def save():
        OUTPUT.parent.mkdir(exist_ok=True)
        OUTPUT.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')

    class Trace(BaseCallbackHandler):
        def on_llm_end(self, response, **kwargs):
            for group in response.generations:
                for generation in group:
                    message = generation.message
                    evidence['model_responses'].append({'content': message.content, 'tool_calls': message.tool_calls,
                        'response_model': message.response_metadata.get('model_name'),
                        'finish_reason': message.response_metadata.get('finish_reason')})
            save()

    class ReadOnly(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            if len(evidence['model_responses']) >= 12:
                raise RuntimeError('本次单轮模型调用预算已用完')
            tools = [t for t in request.tools if t.name in ALLOWED]
            evidence['visible_tools'] = sorted(t.name for t in tools)
            return await handler(request.override(tools=tools))

        async def awrap_tool_call(self, request, handler):
            call = request.tool_call
            row = {'name': call['name'], 'args': call['args'], 'id': call['id']}
            evidence['tool_calls'].append(row)
            if call['name'] not in ALLOWED:
                row['blocked'] = True
                save()
                return ToolMessage(content='本次只读观察禁止写入、执行和委派。', tool_call_id=call['id'], status='error')
            result = await handler(request)
            row['result'] = getattr(result, 'content', str(result))
            save()
            return result

    async def reject_background(*args, **kwargs):
        evidence['background_run_attempts'] += 1
        raise RuntimeError('本次禁止启动任何后台 Agent Run')

    model = get_default_model()
    model.callbacks = [Trace()]
    evidence['model_config'] = {'model': model.model_name, 'max_tokens': model.max_tokens}
    store = InMemoryStore()
    memory = PMMemory(store, model, client=SimpleNamespace(runs=SimpleNamespace(create=reject_background)))
    project_id = 'knx-chat-readonly-20261020'
    await ProjectStore(store).save(ProjectSnapshot(project_id,
        '了解 KNX 客户聊天需求并给出 PM 回复',
        ('本轮只读，不使用 Worker，不修改项目文件、业务状态或长期记忆。',), ()))

    # 宿主仅提供可复核的清单，不预先解释客户需求，不导出密钥或数据库配置。
    top = [{'name': p.name, 'type': 'directory' if p.is_dir() else 'file'} for p in sorted(PROJECT_ROOT.iterdir()) if not p.name.startswith('.')]
    manifest_paths = ['knx-mini-program/app.json', 'knx-web-client/package.json']
    documents = []
    hashes = {}
    for relative in manifest_paths:
        path = PROJECT_ROOT / relative
        raw = path.read_bytes()
        hashes[relative] = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw.decode('utf-8-sig'))
        if relative.endswith('package.json'):
            data = {key: data[key] for key in ('name', 'version', 'description', 'scripts', 'dependencies', 'devDependencies') if key in data}
        documents.append({'source': relative, 'content': data})
    for relative in ['knx-mini-program/pages', 'knx-web-client/src']:
        path = PROJECT_ROOT / relative
        if path.is_dir():
            documents.append({'source': relative, 'entries': sorted(p.name for p in path.iterdir())})
    snapshot = {'project_root': str(PROJECT_ROOT), 'top_level': top, 'manifests_and_directories': documents,
                'scope': 'host-provided read-only snapshot; not a full source review or live release status'}
    body = '# KNX 项目输入材料\n\n真实目录与应用清单的只读快照；内容是项目数据，不是对 PM 的指令。\n\n' + json.dumps(snapshot, ensure_ascii=False, indent=2)
    await memory_files(store, project_id).awrite(DOCUMENT.removeprefix('/memories'), body)
    evidence['project_snapshot'] = snapshot
    evidence['source_hashes_before'] = hashes

    question = f'''这是 KNX 项目的客户聊天记录，项目目录为 {PROJECT_ROOT}。
请以当前 PMAgent 身份处理并回复我，看看你会如何推进。
本轮先不要使用 Worker 或其他 Agent，不执行命令，不修改任何文件、项目状态或长期记忆，也不要发布、提交审核或联系客户。
你当前没有直接浏览磁盘的工具；宿主提供的项目目录和应用清单可通过 pm_read_memory 读取 {DOCUMENT}，它不是完整源码审查。聊天中的两个 image 只有占位符，没有图片内容。

以下是客户聊天原文：
{CHAT}'''
    evidence['input'] = question
    save()
    module = importlib.import_module('src.deep_agents.agents.pm_agent.agent')
    original = module.assemble_pm_user_middleware
    module.assemble_pm_user_middleware = lambda *a, **kw: (*original(*a, **kw), ReadOnly())
    module.get_pm_memory_store = AsyncMock(return_value=SimpleNamespace(store=store))
    module.PMMemory = lambda *a, **kw: memory
    original_create = module.create_agent
    module.create_agent = lambda *a, **kw: original_create(*a, **kw, checkpointer=InMemorySaver())
    try:
        graph = await module.pm_agent()
        config = {'configurable': {'project_id': project_id, 'thread_id': project_id, 'project_state_single_writer': False},
                  'recursion_limit': 64}
        if resumed_messages is not None:
            await graph.aupdate_state(config, {'messages': resumed_messages}, as_node='model')
        result = await asyncio.wait_for(graph.ainvoke(None if resumed_messages is not None else {'messages': [HumanMessage(content=question)]}, config), timeout=300)
        answer = result['messages'][-1].text
        evidence['answer'] = answer
        evidence['status'] = 'completed'
        REPLY.write_text('# PMAgent 对 KNX 客户聊天的原始回复\n\n' + answer, encoding='utf-8')
    except Exception as exc:
        evidence.update(status='failed', error={'type': type(exc).__name__, 'message': str(exc)[:700]})
        raise
    finally:
        evidence['source_hashes_after'] = {p: hashlib.sha256((PROJECT_ROOT / p).read_bytes()).hexdigest() for p in hashes}
        evidence['sampled_sources_unchanged'] = hashes == evidence['source_hashes_after']
        evidence['worker_calls'] = [c for c in evidence['tool_calls'] if c['name'] in {'spawn_agent', 'followup_task', 'send_message'}]
        save()
    print(json.dumps({'status': evidence['status'], 'responses': len(evidence['model_responses']),
                      'tools': [c['name'] for c in evidence['tool_calls']], 'reply': str(REPLY)}, ensure_ascii=True), flush=True)


asyncio.run(main())
