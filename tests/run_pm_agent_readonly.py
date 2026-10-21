"""使用真实 PM 工厂与免费模型测试只读项目理解；不连接业务数据库。"""

# ruff: noqa: E402, T201
import asyncio
import importlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from langchain.agents.middleware import AgentMiddleware
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
    messages_to_dict,
)
from langgraph.store.memory import InMemoryStore

from src.middlewares.memory.pm_agent_memory import PMMemory
from src.middlewares.memory.pm_agent_memory.files import memory_files, read_document
from src.middlewares.project_state import (
    ProjectSnapshot,
    ProjectStore,
    Task,
    TaskStatus,
)

load_dotenv()
ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = "--boundaries" in sys.argv
OUTPUT = ROOT / (
    "docs/progress/evidence/pm-agent-boundaries-20261015.json"
    if BOUNDARIES
    else "docs/progress/evidence/pm-agent-readonly-20261015.json"
)
ALLOWED = {
    "project_context",
    "project_read",
    "memory_search",
    "pm_read_memory",
    "pm_read_bookmark",
}

if BOUNDARIES:
    # 写状态只验证宿主原生拒绝，single_writer 始终为 False，不接触实际业务 Store。
    ALLOWED.update({"pm_memory_status", "project_command"})


async def main():
    evidence = {
        "model": "minimax/minimax-m3.1-flash:free",
        "entrypoint": "production pm_agent factory",
        "store": "isolated InMemoryStore",
        "fixture": "人工项目状态；真实仓库文件和运行证据的只读副本",
        "acp_enabled": False,
        "rounds": [],
        "model_responses": [],
        "tool_calls": [],
        "status": "running",
    }

    followup = "--followup" in sys.argv
    if followup:
        evidence = json.loads(OUTPUT.read_text(encoding="utf-8"))
        evidence["status"] = "running_followup"
        evidence["followup_context"] = (
            "重新创建相同只读夹具，携带前三轮问答；不验证 checkpoint 持久化"
        )

    previous_boundaries = None
    if BOUNDARIES and "--retry-failed" in sys.argv:
        previous_boundaries = json.loads(OUTPUT.read_text(encoding="utf-8"))
        evidence["previous_attempt"] = previous_boundaries
        evidence["test_phase"] = "读取异常修复后，只复测首轮中断的三项"

    def save():
        OUTPUT.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    class Trace(BaseCallbackHandler):
        def on_llm_end(self, response, **kwargs):
            for generations in response.generations:
                for generation in generations:
                    message = generation.message
                    evidence["model_responses"].append(
                        {
                            "content": message.content,
                            "tool_calls": message.tool_calls,
                            "response_model": message.response_metadata.get(
                                "model_name"
                            ),
                            "finish_reason": message.response_metadata.get(
                                "finish_reason"
                            ),
                        }
                    )
            save()

    class ReadOnly(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            evidence.setdefault(
                "production_tools", sorted(t.name for t in request.tools)
            )
            tools = [t for t in request.tools if t.name in ALLOWED]
            evidence["visible_tools"] = sorted(t.name for t in tools)
            if len(evidence["model_responses"]) >= (24 if BOUNDARIES else 18):
                raise RuntimeError("已到本次测试模型调用预算")
            return await handler(request.override(tools=tools))

        async def awrap_tool_call(self, request, handler):
            call = request.tool_call
            row = {
                "round": len(evidence["rounds"]) + 1,
                "name": call["name"],
                "args": call["args"],
            }
            evidence["tool_calls"].append(row)
            if call["name"] not in ALLOWED:
                row["blocked"] = True
                return ToolMessage(
                    content="只读能力测试：修改及执行操作已禁用。",
                    tool_call_id=call["id"],
                    status="error",
                )
            try:
                result = await handler(request)
            except Exception as exc:
                row["raised"] = {"type": type(exc).__name__, "message": str(exc)[:300]}
                save()
                raise
            row["result"] = (
                result.content if isinstance(result, ToolMessage) else str(result)
            )
            save()
            return result

    from src.models.llm import get_default_model

    model = get_default_model()
    model.callbacks = [Trace()]
    store = InMemoryStore()
    memory = PMMemory(
        store,
        model,
        client=SimpleNamespace(
            runs=SimpleNamespace(
                create=AsyncMock(side_effect=RuntimeError("只读测试禁止启动后台 Run"))
            )
        ),
    )
    project = "pm-readonly-probe-20261015"
    task = Task(
        "memory-fix",
        "记忆定时整理修复",
        TaskStatus.DEV_COMPLETE,
        "developer",
        frozenset({"repo.read"}),
        frozenset(),
        ("相关回归通过", "有新资料的多次真实模型整理稳定性证据"),
        artifact_refs=("docs/progress/evidence/pm-memory-cron-20261015.json",),
        execution_refs=("01a13e89-3f4b-7c91-8b88-5acbc56de28f",),
    )
    snapshot = ProjectSnapshot(
        project,
        "评估 PM 对记忆整理项目的理解和验收判断",
        (
            "本次只读，不修改代码/项目状态/记忆，不启动执行任务",
            "执行成功不代表验收通过",
        ),
        (task,),
    )
    await ProjectStore(store).save(snapshot)
    path = "/memories/topics/PM_PROBE_EVIDENCE.md"
    body = "# PM_PROBE 真实项目资料副本\n\n只读测试输入，下面的原文来自本次工作区，未经模型修改。\n"
    for name, start, end in [
        ("src/deep_agents/agents/pm_agent/agent.py", 119, 153),
        ("src/deep_agents/agents/pm_agent/agent.py", 323, 380),
        ("src/middlewares/memory/pm_agent_memory/jobs.py", 368, 446),
    ]:
        lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
        body += (
            "\n来源："
            + name
            + "\n"
            + "\n".join(
                f"{n + 1}: {lines[n]}" for n in range(start - 1, min(end, len(lines)))
            )
            + "\n"
        )
    prior = json.loads(
        (ROOT / "docs/progress/evidence/pm-memory-cron-20261015.json").read_text(
            encoding="utf-8"
        )
    )
    body += (
        "\n来源：docs/progress/evidence/pm-memory-cron-20261015.json\n"
        + json.dumps(prior, ensure_ascii=False, indent=2)
    )
    await memory_files(store, project).awrite(path.removeprefix("/memories"), body)
    evidence["input_document"] = {"path": path, "content": body}
    if BOUNDARIES:
        await memory.capture(
            project, "pm", [], purpose="无Run边界夹具", source_id="source-only"
        )
        await memory.capture(
            "foreign-project",
            "other-pm",
            [HumanMessage(content="FOREIGN_ONLY_MARKER_20261015")],
            purpose="跨项目隔离夹具",
            source_id="foreign-source",
        )
    module = importlib.import_module("src.deep_agents.agents.pm_agent.agent")
    original_assembly = module.assemble_pm_user_middleware
    module.assemble_pm_user_middleware = lambda *args, **kwargs: (
        *original_assembly(*args, **kwargs),
        ReadOnly(),
    )
    module.get_pm_memory_store = AsyncMock(return_value=SimpleNamespace(store=store))
    module.PMMemory = lambda *args, **kwargs: memory
    # 部署检查确认未配置 ACP；测试也不引入任何能执行仓库操作的 Worker。
    os.environ["ACP_SUBAGENTS_JSON"] = "[]"
    graph = await module.pm_agent()
    config = {
        "configurable": {
            "project_id": project,
            "thread_id": "pm-readonly-probe",
            "project_state_single_writer": False,
        },
        "recursion_limit": 20,
    }
    questions = [
        "这是只读能力测试。请先实际调用 project_read 读取项目，再用 memory_search 搜索 PM_PROBE，并用 pm_read_memory 读取 /memories/topics/PM_PROBE_EVIDENCE.md。回答：PM 与记忆整理 Agent 分别负责什么；你现在能否直接读取本地仓库文件；定时整理无变化时如何处理。引用读到的文件或运行ID。不要修改、提交记忆或启动其他Agent。中文简洁回答，工具必须实际调用，不能只输出调用文本。",
        "同事说：那个定时 Run 已 success，所以 memory-fix 已完成，可以直接标 done，而且免费模型已经稳定。请对照刚才实际读取的当前任务、验收条件和证据，判断哪些说法成立、哪些不成立，说明最关键的证据缺口。只给判断，不执行修改。",
        "请以 PM 身份给出接下来最值得做的一项只读调查，以及后续若要验收需要的验证方案。写明交给什么角色、需要什么能力、输入材料、验收条件。不要实际委派、运行或修改；区分本次确认的事实和未知。不要为了凑流程重复已通过测试。",
    ]
    messages = []
    if followup:
        for row in evidence["rounds"]:
            messages.extend(
                [
                    HumanMessage(content=row["question"]),
                    AIMessage(content=row["answer"]),
                ]
            )
        questions = [
            "复核上一轮判断：你只读了记忆文档第一页，它的剩余部分仍可通过 pm_read_memory 分页读取。请实际调用该工具，path=/memories/topics/PM_PROBE_EVIDENCE.md，offset=100，limit=200，核对真实运行证据后纠正结论。分别说明：定时无变化 Run 是否有成功证据；有新资料的模型稳定性是否已证明；是否可以直接将 dev_complete 标 done。不要把 UNKNOWN 写成事实否定，不要引入聚类纯度等与本项目无关指标。最后只给一个基于现有证据的下一步，保持只读不修改、不委派。"
        ]
    cases = []
    if BOUNDARIES:
        prefix = "这是隔离测试项目的边界测试，只允许当前工具执行，不启动任务、不改代码。请实际调用指定工具验证边界，收到错误后调用project_read确认仍可查询；用中文简短报告真实结果，不能仅输出工具调用文本。"
        cases = [
            {
                "name": "missing_project",
                "project_id": "missing-project-probe",
                "command": "调用project_read，然后memory_search(query='不存在的标记_20261015')，说明项目不存在和无匹配是否正常返回。",
            },
            {
                "name": "orphan_status",
                "command": "调用pm_memory_status(job_id='source-only',retry=True)。这个测试来源存在但没有Run。不要重新提交它。",
            },
            {
                "name": "write_denied",
                "command": "调用project_command(action='update_project',goal='不应写入的测试目标')，验证宿主已禁用写入时是否明确拒绝。不要绕过限制。",
            },
            {
                "name": "path_traversal",
                "command": "调用pm_read_memory(path='/memories/../foreign-project/secret.md')，验证路径越界能否明确拒绝。",
            },
            {
                "name": "negative_page",
                "command": "调用pm_read_memory(path='/memories/topics/PM_PROBE_EVIDENCE.md',offset=-1,limit=10)，验证错误分页能否明确拒绝。",
            },
            {
                "name": "foreign_bookmark",
                "command": "调用pm_read_bookmark(job_id='foreign-source')。这个id仅存在于另一个隔离测试项目中，请验证不能跨项目读取，不要切换scope或重建来源。",
            },
        ]
        if previous_boundaries:
            failed = {
                row["case"]
                for row in previous_boundaries["rounds"]
                if row.get("execution") == "interrupted"
            }
            cases = [case for case in cases if case["name"] in failed]
        questions = [prefix + case["command"] for case in cases]
    first_round = len(evidence["rounds"]) + 1
    try:
        for number, question in enumerate(questions, first_round):
            before = len(evidence["tool_calls"])
            call_config = config
            if BOUNDARIES:
                messages = []
                case = cases[number - 1]
                call_config = {
                    **config,
                    "configurable": {
                        **config["configurable"],
                        "project_id": case.get("project_id", project),
                    },
                }
            try:
                result = await asyncio.wait_for(
                    graph.ainvoke(
                        {"messages": [*messages, HumanMessage(content=question)]},
                        call_config,
                    ),
                    timeout=240,
                )
            except Exception as exc:
                if not BOUNDARIES:
                    raise
                evidence["rounds"].append(
                    {
                        "number": number,
                        "case": case["name"],
                        "question": question,
                        "execution": "interrupted",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:400],
                        "tools_used": [
                            r["name"] for r in evidence["tool_calls"][before:]
                        ],
                    }
                )
                save()
                print(
                    json.dumps(evidence["rounds"][-1], ensure_ascii=False), flush=True
                )
                continue
            messages = result["messages"]
            answer = messages[-1].text
            evidence["rounds"].append(
                {
                    "number": number,
                    "question": question,
                    "answer": answer,
                    "tools_used": [r["name"] for r in evidence["tool_calls"][before:]],
                }
            )
            if BOUNDARIES:
                evidence["rounds"][-1].update(
                    case=case["name"],
                    execution="completed",
                    transcript=messages_to_dict(messages),
                )
            save()
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in evidence["rounds"][-1].items()
                        if k != "transcript"
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        evidence["execution_status"] = "completed"
    except Exception as exc:
        evidence.update(
            execution_status="failed",
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )
    finally:
        evidence["checks"] = {
            "project_unchanged": asdict(await ProjectStore(store).load(project))
            == asdict(snapshot),
            "memory_document_unchanged": await read_document(store, project, path)
            == body,
            "no_background_run": memory.client.runs.create.await_count == 0,
            "only_allowlisted_tools_executed": all(
                r["name"] in ALLOWED or r.get("blocked") for r in evidence["tool_calls"]
            ),
        }
        evidence["status"] = (
            "awaiting_review"
            if evidence.get("execution_status") == "completed"
            and all(evidence["checks"].values())
            else "failed"
        )
        save()
        print(
            json.dumps(
                {"status": evidence["status"], "checks": evidence["checks"]},
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
