"""真实PM业务工具自检：隔离内存项目，不派工、不改网站、不签发批准。"""

# ruff: noqa: E402, T201
import asyncio
import importlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from src.middlewares.memory.pm_agent_memory.provider import PMMemoryStore
from src.middlewares.project_state import (
    ProjectSnapshot,
    ProjectStore,
    Task,
    TaskStatus,
)
from src.middlewares.project_state.workflow import ProjectWorkflow

load_dotenv(ROOT / ".env")


async def main():
    identity = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
    directory = ROOT / "workspace/pm-workflow-tools" / identity
    directory.mkdir(parents=True)
    store = InMemoryStore()
    workflow = ProjectWorkflow(store)
    tasks = []
    for task_id, status, dependencies in (
        ("T0_subject", TaskStatus.REVIEW, ()),
        ("T0_review", TaskStatus.BACKLOG, ("T0_subject",)),
    ):
        brief = await workflow.artifacts.create(
            "tool-audit", task_id, "task_brief", "仅用于工具自检，无产品代码。"
        )
        tasks.append(
            Task(
                task_id,
                task_id,
                status,
                "reviewer",
                frozenset(),
                frozenset(dependencies),
                ("工具规则有效",),
                artifact_refs=(brief["reference"],),
            )
        )
    await ProjectStore(store).save(
        ProjectSnapshot("tool-audit", "隔离工具自检", (), tuple(tasks))
    )
    events = []
    failures = {}

    class Trace(AgentMiddleware):
        async def awrap_tool_call(self, request, handler):
            started = time.monotonic()
            result = await handler(request)
            content = getattr(result, "content", None)
            row = {
                "time_utc": datetime.now(UTC).isoformat(),
                "name": request.tool_call["name"],
                "args": request.tool_call["args"],
                "seconds": time.monotonic() - started,
                "status": getattr(result, "status", None),
                "content": content,
            }
            events.append(row)
            (directory / "tools.json").write_text(
                json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if isinstance(result, ToolMessage) and result.status == "error":
                key = json.dumps(
                    [row["name"], row["args"], content],
                    sort_keys=True,
                    ensure_ascii=False,
                )
                failures[key] = failures.get(key, 0) + 1
                if failures[key] > 1:
                    raise RuntimeError("相同参数与错误重复出现，停止工具专项并保留证据")
            return result

    module = importlib.import_module("src.deep_agents.agents.pm_agent.agent")

    def traced_agent(*args, **kwargs):
        kwargs["middleware"] = (Trace(), *kwargs["middleware"])
        kwargs["checkpointer"] = InMemorySaver()
        return create_agent(*args, **kwargs)

    # 工厂、身份、项目/记忆/审批中间件及模型配置均复用生产入口。
    # 本专项不启动外部Worker服务，ACP仅在本进程关闭；Store为明确标注的临时替身。
    with (
        patch.dict(os.environ, {"ACP_SUBAGENTS_JSON": ""}),
        patch.object(
            module,
            "get_pm_memory_store",
            AsyncMock(return_value=PMMemoryStore(store, "memory", "ephemeral")),
        ),
        patch.object(module, "create_agent", traced_agent),
    ):
        graph = await module.pm_agent()
    prompt = """请协助进行一次隔离的项目工具自检，项目已预置T0_subject（review）和T0_review（backlog，错误地依赖T0_subject完成）。
本次只测试业务管理工具，不调查文件，不调用记忆整理，不委派Worker，也不处理真实产品。
请实际调用工具完成这些检查：
1. 读取T0_review，故意尝试一次ready，检查错误是否指出依赖及当前状态，然后通过依赖更新入口修正，再推进ready、assigned。
2. 用T0_NEW创建一项新任务（先建说明制品），保持原大小写编号，不派工、不创建同名副本；检查创建响应和单任务读取是否简洁清楚。
3. 故意尝试一次将T0_subject推进passed，确认缺少可信验收时被拒绝。不要猜其他状态或伪造记录。
最后直接向我汇报每项实际工具结果、遇到的缺陷和需要宿主提供什么。不要请求人工决定工具，不需要真实批准，不要为了完成自检放宽门禁。"""
    followup = "--followup" in sys.argv
    if followup:
        prompt = """本次仅核验上轮工具自检发现的剩余问题，不重复创建任务或改依赖。项目已预置T0_subject处于review、T0_review处于backlog且依赖T0_subject的done。
请实际调用工具：将T0_subject推进testing，再故意尝试一次passed（无可信验收，应拒绝），然后转回review；对T0_review故意尝试一次ready（依赖未完成，应拒绝）。
检查两个错误的code、reason、next_action能否区分验收门禁和依赖阻塞，检查review/testing互转是否可行。完成后直接汇报，禁止派工、读文件、记忆整理、伪造验收、请求真实批准或反复重试。"""
    started = time.monotonic()
    report = {
        "run_directory": str(directory),
        "started_utc": datetime.now(UTC).isoformat(),
        "scope": "real PM factory/model/tools; ephemeral InMemoryStore; ACP disabled in test process",
        "prompt": prompt,
    }
    try:
        result = await graph.ainvoke(
            {"messages": [("human", prompt)]},
            {
                "configurable": {
                    "thread_id": "tool-audit-" + identity,
                    "project_id": "tool-audit",
                    "project_state_single_writer": True,
                },
                "recursion_limit": 2147483647,
            },
        )
        report["messages"] = [
            message.model_dump(mode="json") for message in result["messages"]
        ]
        report["status"] = "finished"
    except Exception as error:
        report.update(
            status="stopped_on_error",
            error={"type": type(error).__name__, "message": str(error)},
        )
    report["seconds"] = time.monotonic() - started
    report["instrumented_calls"] = len(events)
    report["tool_calls"] = sum(
        len(message.get("tool_calls", [])) for message in report.get("messages", [])
    )
    snapshot = await ProjectStore(store).load("tool-audit")
    report["tasks"] = [
        {
            "task_id": task.task_id,
            "status": task.status.value,
            "dependencies": sorted(task.dependencies),
        }
        for task in snapshot.tasks
    ]
    errors = [
        json.loads(message["content"])["error"]
        for message in report.get("messages", [])
        if message.get("type") == "tool" and message.get("status") == "error"
    ]
    if report["status"] == "finished":
        statuses = {task.task_id: task.status for task in snapshot.tasks}
        checks = {"subject_not_accepted": statuses["T0_subject"] is TaskStatus.REVIEW}
        if followup:
            checks["classified_errors"] = {error["code"] for error in errors} == {
                "validation_required",
                "dependencies_blocked",
            }
            checks["review_testing_roundtrip"] = any(
                event["args"].get("target_status") == "testing"
                and event["status"] == "success"
                for event in events
            )
        else:
            checks["dependency_repaired"] = (
                statuses["T0_review"] is TaskStatus.ASSIGNED
                and not snapshot.tasks[1].dependencies
            )
            checks["identity_preserved"] = set(statuses) == {
                "T0_subject",
                "T0_review",
                "T0_NEW",
            }
        report["checks"] = checks
        report["status"] = "passed" if all(checks.values()) else "failed_checks"
    path = directory / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: report[key] for key in ("status", "seconds", "tool_calls", "tasks")},
            ensure_ascii=True,
        ),
        flush=True,
    )
    print(str(path), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
