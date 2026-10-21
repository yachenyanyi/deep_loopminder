"""真实 PM 派工、ACP 交付、正常重启与验收门禁测试；不修改生产配置。"""

# ruff: noqa: E402, T201
import asyncio
import hashlib
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from langchain.agents.middleware import AgentMiddleware
from langchain_core.callbacks import BaseCallbackHandler
from langgraph_sdk import get_client

load_dotenv(ROOT / ".env")
OUTPUT = ROOT / "docs/progress/evidence/pm-project-flow-repaired-20261015.json"
_model = None


def append_trace(name, value):
    """仅记录测试输入和公开运行信息，不记录模型配置或认证数据。"""
    with (Path(os.environ["PM_FLOW_DIRECTORY"]) / name).open(
        "a", encoding="utf-8"
    ) as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


class ModelTrace(BaseCallbackHandler):
    def on_llm_end(self, response, **kwargs):
        for group in response.generations:
            for generation in group:
                message = generation.message
                append_trace(
                    "model.jsonl",
                    {
                        "content": message.content,
                        "tool_calls": message.tool_calls,
                        "response_model": message.response_metadata.get("model_name"),
                    },
                )


class ToolTrace(AgentMiddleware):
    async def awrap_model_call(self, request, handler):
        trace = Path(os.environ["PM_FLOW_DIRECTORY"]) / "model.jsonl"
        if trace.exists() and len(trace.read_text(encoding="utf-8").splitlines()) >= 40:
            raise RuntimeError("达到本次测试模型调用上限")
        # 保留真实项目、审批和ACP工具，屏蔽模型主动提交不相关记忆任务。
        tools = [
            tool
            for tool in request.tools
            if tool.name not in {"submit_pm_memory", "pm_investigate_memory"}
        ]
        return await handler(request.override(tools=tools))

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        append_trace(
            "pm-tools.jsonl",
            {
                "event": "start",
                "id": call["id"],
                "name": call["name"],
                "args": call["args"],
            },
        )
        result = await handler(request)
        append_trace(
            "pm-tools.jsonl",
            {
                "event": "returned",
                "id": call["id"],
                "name": call["name"],
                "content": getattr(result, "content", None),
                "status": getattr(result, "status", None),
            },
        )
        return result


def free_model():
    global _model
    if _model is None:
        from src.models.llm import get_default_model

        _model = get_default_model()
        _model.callbacks = [ModelTrace()]
    return _model


async def pm_flow_agent():
    """复用真实PM工厂与模型配置，只追加调用追踪。"""
    free_model()
    module = importlib.import_module("src.deep_agents.agents.pm_agent.agent")
    original = module.assemble_pm_user_middleware
    module.assemble_pm_user_middleware = lambda *args, **kwargs: (
        *original(*args, **kwargs),
        ToolTrace(),
    )
    try:
        return await module.pm_agent()
    finally:
        module.assemble_pm_user_middleware = original


async def flow_memory_agent():
    free_model()
    from src.middlewares.memory.pm_agent_memory.graph import pm_memory_agent

    return await pm_memory_agent()


# Worker生命周期和路由仍复用生产server，测试只增加原生读取事件追踪。
from src.middlewares.acp import server as acp_server


async def observe_native(event):
    if event.update.session_update in {"tool_call", "tool_call_update"}:
        value = event.model_dump(mode="json", by_alias=True, exclude_none=True)
        update = value["update"]
        append_trace(
            "native-tools.jsonl",
            {
                "session_id": value["sessionId"],
                **{
                    key: update[key]
                    for key in (
                        "sessionUpdate",
                        "toolCallId",
                        "title",
                        "kind",
                        "status",
                        "locations",
                        "rawInput",
                    )
                    if key in update
                },
            },
        )


if not hasattr(acp_server, "_pm_flow_original_worker"):
    acp_server._pm_flow_original_worker = acp_server.ACPWorker

    def instrumented_worker(*args, **kwargs):
        return acp_server._pm_flow_original_worker(
            *args, **kwargs, on_update=observe_native
        )

    acp_server.ACPWorker = instrumented_worker
app = acp_server.app
acp_worker = acp_server.acp_worker
acp_control = acp_server.acp_control


async def main():
    """每阶段保存实际结果，测试结束关闭服务；强制终止不冒充正常恢复。"""
    from urllib.parse import urlparse

    uri = os.getenv("LANGGRAPH_POSTGRES_URI", "")
    if urlparse(uri).path != "/deep_loopminder_test":
        raise RuntimeError("只允许既有独立测试库 deep_loopminder_test")
    adapter = Path(
        "C:/Users/User/AppData/Local/npm-cache/_npx/d820eb7d96bc2600/node_modules/@agentclientprotocol/claude-agent-acp/dist/index.js"
    )
    if not adapter.exists():
        raise RuntimeError("既有Claude ACP适配器不存在")
    directory = Path(tempfile.mkdtemp(prefix="pm-project-flow-"))
    snapshot = directory / "snapshot"
    snapshot.mkdir()
    manifest = {}
    for relative in (
        "src/deep_agents/agents/pm_agent/agent.py",
        "src/middlewares/project_state/tools.py",
    ):
        source = ROOT / relative
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        manifest[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
    settings = snapshot / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {
                "permissions": {
                    "defaultMode": "plan",
                    "allow": ["Read", "Glob", "Grep"],
                    "deny": [
                        "Bash",
                        "Edit",
                        "Write",
                        "NotebookEdit",
                        "Agent",
                        "Task",
                        "WebFetch",
                        "WebSearch",
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    brief = snapshot / "task-brief.md"
    brief.write_text(
        """# 验收接线只读调查
目标：检查当前PM默认入口是否绑定可信facts_provider，以及passed/approved的程序条件。
只读取本任务说明、src/deep_agents/agents/pm_agent/agent.py、src/middlewares/project_state/tools.py。
交付：简短中文报告，写出两个文件中可核查的行号、结论和待接入项，结尾含 FLOW_READCHECK_20261015。
约束：不得写代码、计划、文档，不执行shell，不切换模式，不修改项目状态。最多读取3个文件，读完直接答复。
验收：父调用能核对原生读取和代码依据；Worker执行成功不代表业务验收通过。
""",
        encoding="utf-8",
    )
    before_files = {
        p.relative_to(snapshot).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in snapshot.rglob("*")
        if p.is_file()
    }
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    project = "pm-flow-" + uuid4().hex[:12]
    task_id = "inspect_acceptance"
    env = dict(os.environ)
    env.update(
        PM_FLOW_DIRECTORY=str(directory),
        LANGGRAPH_API_URL=url,
        PYTHONUTF8="1",
        LANGSMITH_TRACING="false",
    )
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["ACP_SUBAGENTS_JSON"] = json.dumps(
        [
            {
                "name": "reviewer",
                "description": "只读核对项目源码与验收条件",
                "graph_id": "acp_worker",
            }
        ]
    )
    env["ACP_WORKERS_JSON"] = json.dumps(
        [
            {
                "provider_id": "claude",
                "roles": ["reviewer"],
                "capabilities": ["repo.read"],
                "cwd": str(snapshot),
                "command": ["J:/dev_soft/node/node.exe", str(adapter)],
                "timeout": 240,
                "env": {
                    "CLAUDE_CODE_EXECUTABLE": "C:/Users/User/.local/bin/claude.exe",
                },
                "grants": [
                    "worker.connect",
                    "worker.new",
                    "worker.resume",
                    "worker.ask",
                    "worker.cancel",
                    "worker.observe",
                    "worker.steer",
                    "external_tool.read",
                    "external_tool.search",
                ],
                "approval": {
                    "use": "src.middlewares.approval.builtin:AllowlistProvider",
                    "config": {"allowed_tools": ["acp.read", "acp.search"]},
                },
            }
        ],
        ensure_ascii=False,
    )
    config_path = directory / "langgraph.json"
    # 模块名导入确保http app和图共用同一份官方Worker注册表。
    env["PYTHONPATH"] = str(ROOT / "tests") + os.pathsep + env["PYTHONPATH"]
    config_path.write_text(
        json.dumps(
            {
                "dependencies": [str(ROOT)],
                "graphs": {
                    "pm_agent": "run_pm_project_flow:pm_flow_agent",
                    "pm_memory_agent": "run_pm_project_flow:flow_memory_agent",
                    "acp_worker": "run_pm_project_flow:acp_worker",
                    "acp_control": "run_pm_project_flow:acp_control",
                },
                "http": {"app": "run_pm_project_flow:app"},
                "store": {
                    "path": "src.middlewares.memory.pm_agent_memory.provider.generate_pm_store"
                },
            }
        ),
        encoding="utf-8",
    )
    evidence = {
        "project_id": project,
        "task_id": task_id,
        "model": "minimax/minimax-m3.1-flash:free",
        "url": url,
        "directory": str(directory),
        "task_brief": brief.as_posix(),
        "steps": {},
        "status": "running",
        "scope": "真实PM/ACP/官方Server/独立PG测试库；PM创建Store制品，业务任务自动绑定；审批输入为显式测试夹具，Worker只读源码副本。",
    }
    if OUTPUT.exists():
        evidence["previous_attempt"] = json.loads(OUTPUT.read_text(encoding="utf-8"))
    process = None
    log = (directory / "server.log").open("a", encoding="utf-8")
    client = get_client(url=url, timeout=360)

    def save(name, data):
        evidence["steps"][name] = data
        OUTPUT.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({"step": name, **data}, ensure_ascii=False), flush=True)

    async def start_server():
        nonlocal process
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-X",
            "utf8",
            "-c",
            """import sys, threading, uvicorn
original_run=uvicorn.run
def controlled_run(app, **kwargs):
    server=uvicorn.Server(uvicorn.Config(app, **kwargs))
    def stop_on_input():
        sys.stdin.readline()
        server.should_exit=True
    threading.Thread(target=stop_on_input,daemon=True).start()
    server.run()
uvicorn.run=controlled_run
from langgraph_cli.cli import cli
cli()
""",
            "dev",
            "--config",
            str(config_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-browser",
            "--no-reload",
            "--n-jobs-per-worker",
            "6",
            "--allow-blocking",
            cwd=directory,
            env=env,
            stdout=log,
            stderr=log,
            stdin=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        import httpx

        async with httpx.AsyncClient() as http:
            for _ in range(90):
                if process.returncode is not None:
                    raise RuntimeError("测试Server启动失败，请查本地server.log")
                try:
                    if (await http.get(url + "/ok", timeout=1)).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
        raise TimeoutError("测试Server启动超时")

    async def stop_server():
        if process and process.returncode is None:
            process.stdin.write(b"\n")
            await process.stdin.drain()
            try:
                await asyncio.wait_for(process.wait(), 30)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise RuntimeError(
                    "测试Server未正常停止，不能认定恢复测试成功"
                ) from None

    async def pm_turn(thread, name, prompt):
        run = await client.runs.create(
            thread,
            "pm_agent",
            input={"messages": [{"role": "user", "content": prompt}]},
            config={
                "configurable": {
                    "project_id": project,
                    "project_state_single_writer": True,
                },
                "recursion_limit": 120,
            },
            multitask_strategy="enqueue",
        )
        save(name + "_started", {"run_id": run["run_id"]})
        await asyncio.wait_for(client.runs.join(thread, run["run_id"]), timeout=420)
        finished = await client.runs.get(thread, run["run_id"])
        state = await client.threads.get_state(thread)
        value = state["values"]
        answer = value.get("messages", [{}])[-1].get("content", "")
        save(
            name,
            {
                "run_id": run["run_id"],
                "status": finished["status"],
                "answer": answer,
                "interrupts": state.get("interrupts"),
                "async_tasks": value.get("async_tasks", {}),
                "messages": value.get("messages", []),
            },
        )
        if finished["status"] != "success":
            raise RuntimeError("PM阶段未完成：" + name + ":" + finished["status"])
        return value

    try:
        save(
            "starting",
            {"worker": "real Claude ACP", "store": "isolated PostgreSQL test database"},
        )
        await start_server()
        thread = (await client.threads.create())["thread_id"]
        evidence["thread_id"] = thread
        state = await pm_turn(
            thread,
            "delegate",
            "这是隔离测试项目。请真实使用工具逐步执行，每次只调用一个写工具：先project_command create_project，目标为核对PM验收接线，约束只读源码；然后project_create_artifact(task_id='inspect_acceptance',kind='task_brief',content=下述完整任务说明)，必须使用该工具返回的reference创建任务，不能使用本机路径或自造引用。再project_command create_task，task_id=inspect_acceptance，title=核对默认验收接线，owner_role=reviewer，required_capabilities=['repo.read']，验收条件恰好为['报告提供两个源码文件和可核查行号','提供原生读取证据']。将任务按ready、assigned、in_progress的顺序逐步推进，每次等待工具结果。然后spawn_agent(agent_type='reviewer',task_name='inspect_acceptance',message='根据系统注入的当前TaskBrief只读核查两个源码，提供文件行号和FLOW_READCHECK_20261015标记，不写入、不执行shell、不退出plan模式。')。创建后直接返回agent_id，不等待、不标完成、不请求人工批准。工具调用必须实际发生。任务说明正文：\n"
            + brief.read_text(encoding="utf-8")
            .replace("只读取本任务说明、", "说明已通过Store制品注入；只读取")
            .replace("最多读取3个文件", "最多读取两个源码文件"),
        )
        tasks = state.get("async_tasks", {})
        if len(tasks) != 1:
            raise RuntimeError("PM未创建唯一真实Worker，停止，不替PM补派工")
        child = (
            next(iter(tasks.values()))["thread_id"]
            if "thread_id" in next(iter(tasks.values()))
            else next(iter(tasks))
        )
        evidence["child_thread_id"] = child
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            runs = await client.runs.list(child, limit=10)
            if runs and runs[0]["status"] not in {"pending", "running"}:
                save(
                    "worker_execution",
                    {"run_id": runs[0]["run_id"], "status": runs[0]["status"]},
                )
                if runs[0]["status"] != "success":
                    raise RuntimeError("真实Worker执行未成功")
                break
            await asyncio.sleep(3)
        else:
            raise TimeoutError("真实Worker未在期限内完成")
        before = await client.threads.get_state(thread)
        (directory / "parent-after-delegate.json").write_text(
            json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        worker_state = await client.threads.get_state(child)
        (directory / "worker-final-checkpoint.json").write_text(
            json.dumps(worker_state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await stop_server()
        await start_server()
        restored = await client.threads.get_state(thread)
        save(
            "restart",
            {
                "parent_thread_restored": restored["values"].get("async_tasks")
                == before["values"].get("async_tasks"),
                "messages_restored": len(restored["values"].get("messages", []))
                == len(before["values"].get("messages", [])),
            },
        )
        await pm_turn(
            thread,
            "collect",
            "测试服务已正常重启。请实际调用wait_agent(targets=['inspect_acceptance'],timeout_ms=30000)领取刚才Worker结果，必要时list_agents核对。引用真实代码依据，说明结果及尚未验收的事项；不要修改状态、不创建新Worker、不重新执行工作，也不要请求人工批准。",
        )
        await pm_turn(
            thread,
            "acceptance_gate",
            "现在核对默认验收闭环：将inspect_acceptance从in_progress按合法顺序推进到dev_complete、review，每步只发一个project_command；然后尝试transition_task target_status='passed'核对可信验收门禁。若宿主未配置可信验收事实，保留review并明确报告阻塞，不跳过、不伪造批准、不调用人工决定工具，也不修改验收条件。最后project_read核对实际状态。",
        )
        from src.middlewares.memory.pm_agent_memory.provider import generate_pm_store
        from src.middlewares.project_state import ProjectStore

        # 与Server使用同一官方同步Store适配，保持Windows子进程所需的默认Proactor循环。
        async with generate_pm_store() as store:
            snapshot_state = await ProjectStore(store).load(project)
        save(
            "business_state",
            {
                "goal": snapshot_state.goal if snapshot_state else None,
                "tasks": [
                    {
                        "task_id": task.task_id,
                        "status": str(task.status),
                        "execution_refs": task.execution_refs,
                        "artifact_refs": task.artifact_refs,
                    }
                    for task in snapshot_state.tasks
                ]
                if snapshot_state
                else [],
            },
        )
        import re

        from src.middlewares.project_state.workflow import ProjectWorkflow

        async with generate_pm_store() as store:
            workflow = ProjectWorkflow(store)
            snapshot_state = await ProjectStore(store).load(project)
            current_task = snapshot_state.tasks[0]
            result_refs = []
            for reference in current_task.artifact_refs:
                artifact = await workflow.artifacts.get(
                    project, reference, task_id=task_id
                )
                if artifact["kind"] == "worker_result":
                    result_refs.append(reference)
            if len(result_refs) != 1 or not current_task.execution_refs:
                raise RuntimeError("运行或真实交付物没有自动关联业务任务")
            result_artifact = await workflow.artifacts.get(project, result_refs[0])
            report = json.loads(result_artifact["content"])["result"]
            native = [
                json.loads(line)
                for line in (directory / "native-tools.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            completed = {
                event["toolCallId"]
                for event in native
                if event.get("status") == "completed"
            }
            paths = {
                str(Path(event["rawInput"]["file_path"]).resolve())
                for event in native
                if event["toolCallId"] in completed
                and event.get("rawInput", {}).get("file_path")
            }
            expected_sources = [snapshot / name for name in manifest]
            read_verified = all(
                str(path.resolve()) in paths for path in expected_sources
            )
            # 文件名附近出现任意数字不能证明行号可核查；要求引用本次核查的
            # 两个实际接线表达式及其源码行号。这只验证明确代码锚点，不评判全文质量。
            required_anchors = {
                "agent.py": "ProjectStateMiddleware(workflow=ProjectWorkflow(store))",
                "tools.py": "facts = await facts_provider(snapshot, task_id, target_status)",
            }
            anchor_checks = {}
            for path in expected_sources:
                expression = required_anchors[path.name]
                line = next(
                    index
                    for index, text in enumerate(
                        path.read_text(encoding="utf-8").splitlines(), 1
                    )
                    if expression in text
                )
                anchor_checks[path.name] = (
                    path.name in report
                    and expression in report
                    and bool(re.search(rf"\b{line}\b", report))
                )
            anchors = all(anchor_checks.values())
            # 宿主按事先固定的两条验收条件独立核对；不采纳Worker的自报通过。
            checks = {
                "报告提供两个源码文件和可核查行号": bool(anchors),
                "提供原生读取证据": read_verified,
            }
            save(
                "host_validation_checks",
                {
                    "criteria": checks,
                    "execution_refs": current_task.execution_refs,
                    "evidence_refs": result_refs,
                    "brief_valid": True,
                    "source_anchor_checks": anchor_checks,
                },
            )
            if not all(checks.values()):
                raise RuntimeError("独立宿主验收没有满足预定条件，停止收口")
            validation_ref = await workflow.record_validation(
                project,
                task_id,
                validator_id="trusted-host:source-read-checker",
                criteria=checks,
                evidence_refs=tuple(result_refs),
            )
            save(
                "validation_receipt",
                {"reference": validation_ref, "writer": "trusted host, not model"},
            )
        await pm_turn(
            thread,
            "validated",
            "可信宿主已写入当前版本的逐条验收记录。请实际尝试passed，再尝试approved验证人工门禁，缺少人工批准时保持passed，不得伪造批准；最后project_read核对。不要请求人工决定工具，这是显式审批夹具的下一阶段。",
        )
        async with generate_pm_store() as store:
            workflow = ProjectWorkflow(store)
            current = await ProjectStore(store).load(project)
            if current.tasks[0].status.value != "passed":
                raise RuntimeError("默认验收回调未让当前已验证任务进入passed")
            await workflow.record_approval(
                project,
                task_id,
                actor_id="test-fixture-user",
                decision_ref="test-fixture://" + project + "/explicit-approve",
            )
            save(
                "approval_fixture",
                {
                    "synthetic": True,
                    "meaning": "仅隔离测试的明确审批输入，不代表真实业务用户批准",
                },
            )
        await pm_turn(
            thread,
            "closed",
            "本隔离测试的明确人工审批夹具已由宿主写入。请实际依次approved、done，再project_read核对；不能修改任何验收条件或伪造新证据。",
        )
        async with generate_pm_store() as store:
            snapshot_state = await ProjectStore(store).load(project)
            save(
                "final_business_state",
                {
                    "task_id": task_id,
                    "status": snapshot_state.tasks[0].status.value,
                    "execution_refs": snapshot_state.tasks[0].execution_refs,
                    "artifact_refs": snapshot_state.tasks[0].artifact_refs,
                },
            )
            if snapshot_state.tasks[0].status.value != "done":
                raise RuntimeError("已提供有效验收和明确审批夹具后仍未收口")

        final_state = await client.threads.get_state(thread)
        (directory / "parent-latest-checkpoint.json").write_text(
            json.dumps(final_state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        evidence["status"] = "tested_pending_review"
        save(
            "completed",
            {
                "full_project_closure": True,
                "reason": "真实交付经宿主核验、明确审批测试夹具后由默认事实回调解锁；不是模型自批",
            },
        )
    except Exception as exc:
        evidence["status"] = "blocked"
        save("failure", {"type": type(exc).__name__, "error": str(exc)[:400]})
    finally:
        try:
            await stop_server()
            evidence["server_stopped"] = True
        except Exception as exc:
            evidence["server_stopped"] = False
            save("cleanup_failure", {"type": type(exc).__name__})
        log.close()
        from src.deep_agents.db import cleanup_postgres

        await cleanup_postgres()
        after = {
            p.relative_to(snapshot).as_posix(): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in snapshot.rglob("*")
            if p.is_file()
        }
        evidence["snapshot_unchanged"] = before_files == after
        evidence["source_unchanged"] = all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
            for name, digest in manifest.items()
        )
        for name in ("native-tools.jsonl", "pm-tools.jsonl", "model.jsonl"):
            trace = directory / name
            if trace.exists():
                evidence[name] = [
                    json.loads(line)
                    for line in trace.read_text(encoding="utf-8").splitlines()
                ]
        OUTPUT.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "status": evidence["status"],
                    "server_stopped": evidence["server_stopped"],
                    "snapshot_unchanged": evidence["snapshot_unchanged"],
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
