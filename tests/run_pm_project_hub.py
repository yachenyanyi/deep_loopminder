"""一次真实 PM 项目交付观察：只提供客户题目，记录过程，不干预产物。"""

# ruff: noqa: E402, T201
import asyncio
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from dotenv import load_dotenv
from langchain.agents.middleware import AgentMiddleware
from langchain_core.callbacks import BaseCallbackHandler
from langgraph_sdk import get_client

load_dotenv(ROOT / ".env")
from src.middlewares.trajectory import TrajectoryMiddleware, TrajectoryRecorder

_trajectory = None


def trajectory():
    """同一Server进程内共享轨迹，不与官方调度器保存重复生命周期。"""
    global _trajectory
    if _trajectory is None:
        _trajectory = TrajectoryRecorder(os.environ["PROJECT_HUB_RUN_DIRECTORY"])
    return _trajectory


def stamp():
    return datetime.now(UTC).isoformat()


def pending_host_requests(snapshot):
    """读取官方检查点的人工中断，不把Run success当作交付或审批通过。"""
    pending = list(snapshot.get("interrupts", []))
    for task in snapshot.get("tasks", []):
        pending.extend(task.get("interrupts", []))
    return list({json.dumps(item, sort_keys=True): item for item in pending}.values())


def host_resume_command(snapshot, response=None):
    """人工中断必须通过官方respond恢复，不能用input=None反复重入。"""
    pending = pending_host_requests(snapshot)
    if not pending:
        if response is not None:
            raise ValueError("当前检查点没有人工请求，不能投递人工响应")
        return None
    if len(pending) != 1 or not response or not response.strip():
        raise ValueError("需为唯一待处理人工请求提供明确响应；未创建Run")
    actions = pending[0].get("value", {}).get("action_requests", [])
    if len(actions) != 1 or actions[0].get("name") != "request_human_decision":
        raise ValueError("此入口只响应单个request_human_decision，不批准其他工具")
    return {"resume": {"decisions": [{"type": "respond", "message": response}]}}


async def cancel_observed_run(client, thread_id, run_id):
    """取消与结束有竞态；404/409必须重新读取官方状态，不能仅凭错误码忽略。"""
    requested = True
    try:
        await client.runs.cancel(thread_id, run_id, action="interrupt", wait=True)
    except httpx.HTTPStatusError as error:
        if error.response.status_code not in {404, 409}:
            raise
        current = await client.runs.get(thread_id, run_id)
        if current["status"] in {"pending", "running"}:
            raise error
        requested = False
    else:
        current = await client.runs.get(thread_id, run_id)
    return {
        "thread_id": thread_id,
        "run_id": run_id,
        "status": current["status"],
        "cancel_requested": requested,
    }


def failed_worker_runs(all_runs, historical_errors=()):
    """续跑前已确认终止的旧错误只作历史证据；新的失败Run仍触发停止。"""
    return [
        run["run_id"]
        for rows in all_runs.values()
        for run in rows
        if run["status"] == "error" and run["run_id"] not in historical_errors
    ]


def record(name, data):
    directory = Path(os.environ["PROJECT_HUB_RUN_DIRECTORY"])
    with (directory / name).open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps({"time_utc": stamp(), **data}, ensure_ascii=False, default=str)
            + "\n"
        )
    if name == "workers.jsonl":
        phase = {"turn_start": "start", "turn_end": "end", "turn_error": "error"}.get(
            data["event"], "message"
        )
        row = trajectory().emit(
            "worker",
            phase,
            data.get("action") or data["event"],
            agent=data["provider"],
            span_id=data.get("turn_id"),
            thread_id=data.get("session_id", ""),
            duration_ms=data.get("duration_seconds", 0) * 1000,
            data=data,
        )
        if phase == "error":
            trajectory().fail(row)


def fatal_dispatch_errors(value):
    """识别结构化致命错误，不把普通业务校验失败当成框架崩溃。"""
    if isinstance(value, dict):
        if value.get("fatal") is True:
            return [value]
        return [
            error for item in value.values() for error in fatal_dispatch_errors(item)
        ]
    if isinstance(value, (list, tuple)):
        return [error for item in value for error in fatal_dispatch_errors(item)]
    if isinstance(value, str):
        try:
            return fatal_dispatch_errors(json.loads(value))
        except (ValueError, TypeError):
            return []
    if hasattr(value, "update") and isinstance(value.update, dict):
        return fatal_dispatch_errors(value.update)
    if hasattr(value, "content"):
        return fatal_dispatch_errors(value.content)
    return []


class ModelTrace(BaseCallbackHandler):
    """只记录可见消息、调用参数与时间，不采集模型隐藏思考。"""

    def __init__(self):
        self.starts = {}
        self.background = set()

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self.starts[str(run_id)] = time.monotonic()
        metadata = kwargs.get("metadata") or {}
        if metadata.get("graph_id") == "pm_memory_agent" or metadata.get(
            "langgraph_node"
        ) not in {None, "model"}:
            self.background.add(str(run_id))
            trajectory().emit(
                "model",
                "start",
                "后台记忆模型",
                agent="memory",
                span_id=str(run_id),
                data={"messages": messages},
            )
        record(
            "model-events.jsonl",
            {
                "event": "start",
                "call_id": str(run_id),
                "tags": kwargs.get("tags"),
                "metadata": kwargs.get("metadata"),
            },
        )

    def on_llm_end(self, response, *, run_id, **kwargs):
        started = self.starts.pop(str(run_id), None)
        for group in response.generations:
            for generation in group:
                message = generation.message
                record(
                    "model-events.jsonl",
                    {
                        "event": "end",
                        "call_id": str(run_id),
                        "duration_seconds": time.monotonic() - started
                        if started
                        else None,
                        "content": message.content,
                        "tool_calls": message.tool_calls,
                        "response_model": message.response_metadata.get("model_name"),
                        "finish_reason": message.response_metadata.get("finish_reason"),
                        "usage": message.usage_metadata,
                    },
                )
                if str(run_id) in self.background:
                    trajectory().emit(
                        "model",
                        "end",
                        "后台记忆模型",
                        agent="memory",
                        span_id=str(run_id),
                        duration_ms=(time.monotonic() - started) * 1000
                        if started
                        else None,
                        data=message,
                    )
        self.background.discard(str(run_id))

    def on_llm_error(self, error, *, run_id, **kwargs):
        record(
            "model-events.jsonl",
            {"event": "error", "call_id": str(run_id), "error": str(error)[:1000]},
        )
        if not isinstance(error, asyncio.CancelledError):
            event = trajectory().emit(
                "model",
                "error",
                "模型回调异常",
                span_id=str(run_id),
                data={"error": str(error)},
            )
            trajectory().fail(event)


def shared_model():
    from src.models.llm import get_default_model

    model = get_default_model()
    if not any(isinstance(c, ModelTrace) for c in (model.callbacks or [])):
        model.callbacks = [*(model.callbacks or []), ModelTrace()]
    return model


class ToolTrace(AgentMiddleware):
    """只记录，不过滤、替换工具或向模型补充提示。"""

    async def awrap_tool_call(self, request, handler):
        started = time.monotonic()
        call = request.tool_call
        record("pm-tools.jsonl", {"event": "start", **call})
        try:
            result = await handler(request)
        except BaseException as error:
            record(
                "pm-tools.jsonl",
                {
                    "event": "error",
                    "id": call["id"],
                    "name": call["name"],
                    "duration_seconds": time.monotonic() - started,
                    "error": str(error)[:1000],
                },
            )
            raise
        value = (
            result.model_dump(mode="json")
            if hasattr(result, "model_dump")
            else str(result)
        )
        record(
            "pm-tools.jsonl",
            {
                "event": "end",
                "id": call["id"],
                "name": call["name"],
                "duration_seconds": time.monotonic() - started,
                "result": value,
            },
        )
        fatal = fatal_dispatch_errors(result)
        if fatal:
            record("framework-errors.jsonl", {"tool": call["name"], "errors": fatal})
            raise RuntimeError("确定性 ACP 错误已停止本轮，详见 framework-errors.jsonl")
        return result


async def observed_pm():
    shared_model()
    module = importlib.import_module("src.deep_agents.agents.pm_agent.agent")
    original = module.assemble_pm_user_middleware
    observer = TrajectoryMiddleware(trajectory(), stop_on_error=True)
    module.assemble_pm_user_middleware = lambda *a, **kw: (
        observer,
        *original(*a, **kw),
        ToolTrace(),
        observer.request_capture(),
    )
    try:
        return await module.pm_agent()
    finally:
        module.assemble_pm_user_middleware = original


async def observed_memory():
    shared_model()
    from src.middlewares.memory.pm_agent_memory.graph import pm_memory_agent

    return await pm_memory_agent()


from src.middlewares.acp import server as acp_server
from src.middlewares.acp.control import ACPWorker


class ObservedWorker(ACPWorker):
    """保留真实 ACP 协议，仅记录会话与每轮原生执行。"""

    async def new(self, **kwargs):
        trajectory().check_stop()
        ref = await super().new(**kwargs)
        record(
            "workers.jsonl",
            {
                "event": "session_created",
                "provider": self.provider_id,
                "ref": ref.to_dict(),
                "metadata": self.observe(ref)["session"],
            },
        )
        return ref

    async def control(self, ref, action, payload=None, **kwargs):
        if action not in {"cancel", "close"}:
            trajectory().check_stop()
        started = time.monotonic()
        turn_id = uuid4().hex
        record(
            "workers.jsonl",
            {
                "event": "turn_start",
                "turn_id": turn_id,
                "provider": self.provider_id,
                "session_id": ref.session_id,
                "task_id": ref.task_id,
                "role": ref.role,
                "action": action,
                "payload": payload,
            },
        )
        try:
            result = await super().control(ref, action, payload, **kwargs)
        except BaseException as error:
            record(
                "workers.jsonl",
                {
                    "event": "turn_error",
                    "turn_id": turn_id,
                    "provider": self.provider_id,
                    "duration_seconds": time.monotonic() - started,
                    "error": str(error)[:1500],
                },
            )
            raise
        value = (
            result.model_dump(mode="json", by_alias=True, exclude_none=True)
            if hasattr(result, "model_dump")
            else str(result)
        )
        record(
            "workers.jsonl",
            {
                "event": "turn_end",
                "turn_id": turn_id,
                "provider": self.provider_id,
                "duration_seconds": time.monotonic() - started,
                "result": value,
            },
        )
        return result


def instrumented_worker(*args, **kwargs):
    provider = args[0]
    # resume会重放同一原生会话的历史通知；仅排除恢复前已留证的失败调用。
    # 仍记录为红色历史事件，新toolCallId或新会话的失败继续触发停止。
    historical = set()
    source = os.getenv("PROJECT_HUB_RESUME_RUN_DIRECTORY")
    seen = set()
    while source:
        directory = Path(source).resolve()
        if directory in seen or not directory.is_relative_to(
            (ROOT / "workspace/project-hub-runs").resolve()
        ):
            break
        seen.add(directory)
        log = directory / "native-events.jsonl"
        if log.exists():
            for line in log.read_text(encoding="utf-8").splitlines():
                item = json.loads(line)
                value = item.get("event", {})
                update = value.get("update", {})
                if (
                    item.get("provider") == provider
                    and update.get("status") == "failed"
                    and update.get("toolCallId")
                ):
                    historical.add((value.get("sessionId"), update["toolCallId"]))
        metadata = directory / "run.json"
        source = (
            json.loads(metadata.read_text(encoding="utf-8"))
            .get("resumed_from", {})
            .get("directory")
            if metadata.exists()
            else None
        )

    async def native(event):
        # Claude/Codex原生工具失败由Worker自行恢复；ACP执行异常仍触发首错停止。
        value = event.model_dump(mode="json", by_alias=True, exclude_none=True)
        replay = (
            value.get("sessionId"),
            value.get("update", {}).get("toolCallId"),
        ) in historical
        trajectory().native(
            provider,
            event,
            stop_on_error=provider not in {"claude", "codex"} and not replay,
        )
        update = event.update
        if update.session_update in {
            "tool_call",
            "tool_call_update",
            "agent_message_chunk",
            "usage_update",
            "plan",
        }:
            record(
                "native-events.jsonl",
                {
                    "provider": provider,
                    "historical_replay": replay,
                    "event": value,
                },
            )

    return ObservedWorker(*args, **kwargs, on_update=native)


if os.getenv("PROJECT_HUB_RUN_DIRECTORY"):
    acp_server.ACPWorker = instrumented_worker
app = acp_server.app
acp_worker = acp_server.acp_worker
acp_control = acp_server.acp_control

TASK = """我们团队同时维护很多客户项目，想做一个网站统一管理。能登记客户和联系人，创建项目，把客户发来的聊天整理成需求，安排负责人，查看进度。项目里能记录问题、讨论和交付物，也能区分待确认、开发中、待测试和已完成的事项。希望首页能看到延期、待回复和最近更新，支持搜索、筛选、归档，以及导出项目进展。先给小团队自己用，页面清楚、操作方便就行。"""


async def main():
    from urllib.parse import urlsplit, urlunsplit

    import httpx
    import psycopg

    resume_directory = os.getenv("PROJECT_HUB_RESUME_RUN_DIRECTORY")
    previous = None
    if resume_directory:
        resume_directory = Path(resume_directory).resolve()
        previous = json.loads(
            (resume_directory / "run.json").read_text(encoding="utf-8")
        )
        if previous["status"] in {"running", "preparing"}:
            raise RuntimeError("原运行仍在执行，不能复制正在写入的检查点")
        if not (resume_directory / ".langgraph_api").is_dir():
            raise RuntimeError("原运行缺少官方持久化检查点，不能声称恢复原线程")
    website = Path(
        os.getenv("PROJECT_HUB_WEBSITE_DIRECTORY")
        or (previous["website"] if previous else None)
        or "C:/Users/User/wt/peoject/pm-project-hub"
    )
    continuing = (
        previous is not None or os.getenv("PROJECT_HUB_CONTINUE_EXISTING") == "true"
    )
    if website.exists() and any(website.iterdir()) and not continuing:
        raise RuntimeError("要求空目录起步，目标已存在；不覆盖")
    website.mkdir(exist_ok=True)
    local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    identity = local_now.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
    directory = ROOT / "workspace/project-hub-runs" / identity
    directory.mkdir(parents=True)
    os.environ["PROJECT_HUB_RUN_DIRECTORY"] = str(directory)
    state = {
        "status": "preparing",
        "preparation_started_utc": stamp(),
        "website": str(website),
        "run_directory": str(directory),
        "initial_directory_empty": not any(website.iterdir()),
        "continuing_existing_product": continuing,
        "host_interventions": [],
        "project_id": previous["project_id"] if previous else "project-hub-" + identity,
        "input": TASK,
    }
    if previous:
        # 复制官方dev存储形成独立续跑分支，旧检查点/首错/轨迹均不覆盖。
        shutil.copytree(
            resume_directory / ".langgraph_api", directory / ".langgraph_api"
        )
        state["resumed_from"] = {
            "directory": str(resume_directory),
            "thread_id": previous["thread_id"],
            "run_id": previous["run_id"],
            "mode": "official_checkpoint_input_none",
        }
    public = ROOT / f"docs/progress/evidence/project-hub-{local_now:%Y%m%d}.json"

    def save():
        text = json.dumps(state, ensure_ascii=False, indent=2)
        temporary = directory / "run.json.tmp"
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(directory / "run.json")
        public.write_text(text, encoding="utf-8")

    save()
    # 独立测试库只做连接检查，不创建/清理业务数据。
    if os.getenv("LANGGRAPH_POSTGRES_URI"):
        uri = urlsplit(os.environ["LANGGRAPH_POSTGRES_URI"])
        test_uri = urlunsplit(uri._replace(path="/deep_loopminder_test"))
    else:
        # 独立测试连接只从当前进程的标准 PG 环境变量获取，不将口令写入文件。
        from psycopg.conninfo import make_conninfo

        test_uri = make_conninfo(
            host=os.getenv("PGHOST", "localhost"),
            port=os.getenv("PGPORT", "5432"),
            user=os.getenv("PGUSER", "postgres"),
            password=os.environ["PGPASSWORD"],
            dbname="deep_loopminder_test",
        )
    with psycopg.connect(test_uri, connect_timeout=10) as connection:
        connection.execute("SELECT 1")
    env = dict(os.environ)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env.update(
        PYTHONUTF8="1",
        PYTHONPATH=str(ROOT / "tests") + os.pathsep + str(ROOT),
        PROJECT_HUB_RUN_DIRECTORY=str(directory),
        LANGGRAPH_POSTGRES_URI=test_uri,
        LANGGRAPH_API_URL=url,
        LANGSMITH_TRACING="false",
        BG_JOB_TIMEOUT_SECS="inf",
        HTTP_PROXY="http://127.0.0.1:7897",
        HTTPS_PROXY="http://127.0.0.1:7897",
        NO_PROXY="localhost,127.0.0.1,::1",
    )
    workers = json.loads(env["ACP_WORKERS_JSON"])
    for worker in workers:
        worker["cwd"] = website.as_posix()
        worker["timeout"] = None
        # 只针对这个空目录开发任务批准常规执行能力，不更改真实部署策略。
        worker["approval"] = {
            "use": "src.middlewares.approval.builtin:AllowlistProvider",
            "config": {
                "allowed_tools": [
                    "acp.read",
                    "acp.search",
                    "acp.edit",
                    "acp.execute",
                    "acp.fetch",
                    "acp.other",
                ]
            },
        }
    env["ACP_WORKERS_JSON"] = json.dumps(workers, ensure_ascii=False)
    config_path = directory / "langgraph.json"
    config_path.write_text(
        json.dumps(
            {
                "dependencies": [str(ROOT)],
                "graphs": {
                    "pm_agent": "run_pm_project_hub:observed_pm",
                    "pm_memory_agent": "run_pm_project_hub:observed_memory",
                    "acp_worker": "run_pm_project_hub:acp_worker",
                    "acp_control": "run_pm_project_hub:acp_control",
                },
                "http": {"app": "run_pm_project_hub:app"},
                "store": {
                    "path": "src.middlewares.memory.pm_agent_memory.provider.generate_pm_store"
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    state["server_url"] = url
    # 网页独立于Agent服务存活，停止后保留回看；没有自动重启/补发任务入口。
    monitor_log = (directory / "monitor.log").open("a", encoding="utf-8")
    monitor = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "src.middlewares.trajectory.web",
            "--directory",
            str(directory),
        ],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=monitor_log,
        stderr=monitor_log,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    while not (directory / "monitor.json").exists():
        if monitor.poll() is not None:
            raise RuntimeError("轨迹网页启动失败，未调用PM")
        await asyncio.sleep(0.1)
    monitor_info = json.loads((directory / "monitor.json").read_text(encoding="utf-8"))
    state.update(
        monitor_url=monitor_info["url"],
        monitor_pid=monitor.pid,
        stop_policy="pm_and_acp_errors_stop_claude_codex_native_tools_observe_only",
    )
    monitor_log.close()
    state["worker_configuration_scope"] = {
        "cwd": website.as_posix(),
        "timeout": None,
        "approval": "allowlisted standard local development tool kinds for this run only",
        "roles": {w["provider_id"]: w["roles"] for w in workers},
    }
    bootstrap = """import sys,threading,uvicorn
def run(app,**kwargs):
 server=uvicorn.Server(uvicorn.Config(app,**kwargs))
 def stop():
  sys.stdin.readline();server.should_exit=True
 threading.Thread(target=stop,daemon=True).start();server.run()
uvicorn.run=run
from langgraph_cli.cli import cli
cli()
"""
    log = (directory / "server.log").open("a", encoding="utf-8")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-X",
        "utf8",
        "-c",
        bootstrap,
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
        stdin=asyncio.subprocess.PIPE,
        stdout=log,
        stderr=log,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    state["server_pid"] = process.pid
    save()
    try:
        async with httpx.AsyncClient(trust_env=False) as http:
            while True:
                if process.returncode is not None:
                    raise RuntimeError("Server 启动失败，检查 server.log；未调用 PM")
                try:
                    if (await http.get(url + "/ok", timeout=3)).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
        client = get_client(url=url, timeout=60)
        historical_errors = set()
        resume_command = None
        if previous:
            thread_id = previous["thread_id"]
            restored = await client.threads.get_state(thread_id)
            if not restored.get("values", {}).get("messages") or not restored.get(
                "next"
            ):
                raise RuntimeError("原PM检查点没有待恢复节点；未创建新Run，需核对现场")
            for worker_id in restored["values"].get("async_tasks", {}):
                historical_errors.update(
                    run["run_id"]
                    for run in await client.runs.list(worker_id, limit=100)
                    if run["status"] == "error"
                )
            state["historical_worker_errors"] = sorted(historical_errors)
            response_file = os.getenv("PROJECT_HUB_HOST_RESPONSE_FILE")
            response = (
                Path(response_file).read_text(encoding="utf-8")
                if response_file
                else None
            )
            resume_command = host_resume_command(restored, response)
            if resume_command:
                state["resumed_from"]["mode"] = "official_hitl_respond"
                state["host_response"] = response
        else:
            thread_id = (await client.threads.create())["thread_id"]
        state["thread_id"] = thread_id
        starting_point = (
            f"项目位于 {website.as_posix()}，保留前轮未完成的开发产物，请接续完成。前轮因临时验证脚本的依赖解析错误被宿主中断，该临时检查现已修复并验证，网站尚未交付。"
            if continuing
            else f"项目从空目录 {website.as_posix()} 开始。"
        )
        prompt = (
            TASK
            + "\n\n请作为项目经理组织并完成这个网站，交付能实际使用的网站，不只给计划或半成品。"
            + starting_point
            + "请记录必要的使用和启动方式。开发由 developer 负责，测试和验收由相应角色负责。宿主只记录过程，不替你拆任务或补实现。"
        )
        # input=None让官方图从失败节点继续，避免追加human消息留下未配对工具调用。
        state["submitted_prompt"] = None if previous else prompt
        state["pm_started_utc"] = stamp()
        started = time.monotonic()
        run = await client.runs.create(
            thread_id,
            "pm_agent",
            input=None
            if previous
            else {"messages": [{"role": "user", "content": prompt}]},
            **({"command": resume_command} if resume_command else {}),
            config={
                "configurable": {
                    "project_id": state["project_id"],
                    "project_state_single_writer": True,
                },
                "recursion_limit": 2147483647,
            },
            multitask_strategy="enqueue",
        )
        state.update(status="running", run_id=run["run_id"])
        save()
        print(
            json.dumps(
                {
                    "started": True,
                    "directory": str(directory),
                    "website": str(website),
                    "thread_id": thread_id,
                    "run_id": run["run_id"],
                    "url": url,
                },
                ensure_ascii=True,
            ),
            flush=True,
        )
        # 不干预产品结果；框架错误立即停止，不按时长或调用次数截断。
        while True:
            current = await client.runs.get(thread_id, run["run_id"])
            snapshot = await client.threads.get_state(thread_id)
            (directory / "pm-checkpoint.json").write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tasks = snapshot.get("values", {}).get("async_tasks", {})
            all_runs = {}
            for worker_id in tasks:
                all_runs[worker_id] = await client.runs.list(worker_id, limit=100)
            (directory / "worker-runs.json").write_text(
                json.dumps(all_runs, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            state.update(
                pm_status=current["status"],
                elapsed_seconds=time.monotonic() - started,
                last_observed_utc=stamp(),
                worker_count=len(tasks),
                worker_run_count=sum(len(v) for v in all_runs.values()),
            )
            save()
            framework_errors = failed_worker_runs(all_runs, historical_errors)
            if (
                (directory / "first-error.json").exists()
                or (directory / "stop-request.json").exists()
                or (directory / "framework-errors.jsonl").exists()
                or framework_errors
                or current["status"] in {"error", "timeout"}
            ):
                state["framework_stop"] = {
                    "detected_utc": stamp(),
                    "failed_worker_runs": framework_errors,
                    "reason": "首个显式错误或网页停止请求：自动停止并保留现场",
                }
                save()
                targets = [(thread_id, current)] + [
                    (key, r) for key, rows in all_runs.items() for r in rows
                ]
                cancellations = await asyncio.gather(
                    *(
                        cancel_observed_run(client, key, active["run_id"])
                        for key, active in targets
                        if active["status"] in {"pending", "running"}
                    ),
                    return_exceptions=True,
                )
                state["cancellation_results"] = [
                    result
                    for result in cancellations
                    if not isinstance(result, Exception)
                ]
                save()
                for error in cancellations:
                    if isinstance(error, Exception):
                        raise error
                current = await client.runs.get(thread_id, run["run_id"])
                snapshot = await client.threads.get_state(thread_id)
                (directory / "pm-checkpoint.json").write_text(
                    json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                for key in all_runs:
                    all_runs[key] = await client.runs.list(key, limit=100)
                (directory / "worker-runs.json").write_text(
                    json.dumps(all_runs, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                break
            running = any(
                r["status"] in {"pending", "running"}
                for rows in all_runs.values()
                for r in rows
            )
            if current["status"] not in {"pending", "running"} and not running:
                break
            await asyncio.sleep(0.5)
        messages = snapshot.get("values", {}).get("messages", [])
        final = next(
            (m.get("content", "") for m in reversed(messages) if m.get("type") == "ai"),
            "",
        )
        (directory / "pm-final.md").write_text(str(final), encoding="utf-8")
        for worker_id in tasks:
            checkpoint = await client.threads.get_state(worker_id)
            (directory / f"worker-{worker_id}.json").write_text(
                json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        files = [
            p.relative_to(website).as_posix()
            for p in website.rglob("*")
            if p.is_file()
            and not any(
                part in {"node_modules", ".git", ".venv"}
                for part in p.relative_to(website).parts
            )
        ]
        (directory / "delivered-files.json").write_text(
            json.dumps(files, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        state.update(
            status="stopped_on_error"
            if state.get("framework_stop")
            else "waiting_for_host"
            if pending_host_requests(snapshot)
            else "finished_observation",
            host_requests=pending_host_requests(snapshot),
            pm_status=current["status"],
            finished_utc=stamp(),
            elapsed_seconds=time.monotonic() - started,
            pm_final=final,
            delivered_file_count=len(files),
        )
        save()
        print(
            json.dumps(
                {
                    "finished": True,
                    "status": current["status"],
                    "files": len(files),
                    "seconds": state["elapsed_seconds"],
                },
                ensure_ascii=True,
            ),
            flush=True,
        )
    except BaseException as error:
        state.update(
            status="observation_error",
            error={"type": type(error).__name__, "message": str(error)[:1500]},
        )
        save()
        raise
    finally:
        if process.returncode is None:
            process.stdin.write(b"\n")
            await process.stdin.drain()
            await process.wait()
        log.close()


if __name__ == "__main__":
    asyncio.run(main())
