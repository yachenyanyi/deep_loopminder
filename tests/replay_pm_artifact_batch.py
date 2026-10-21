"""隔离重放真实PM失败批次：原样工具参数、真实业务工具、确定性模型替身。"""

# ruff: noqa: E402, T201
import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.store.memory import InMemoryStore

from src.middlewares.project_state import (
    ProjectSnapshot,
    ProjectStateMiddleware,
    ProjectStore,
)
from src.middlewares.project_state.workflow import ProjectWorkflow
from src.middlewares.trajectory import TrajectoryMiddleware, TrajectoryRecorder


class ReplayModel(FakeMessagesListChatModel):
    """只重放已有调用，不访问模型服务，不给PM注入新策略。"""

    def bind_tools(self, tools, **kwargs):
        return self


async def main():
    """读取原始失败批次，在独立临时Store中验证制品和快照不变量。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, required=True, help="原始PM checkpoint.json"
    )
    args = parser.parse_args()
    checkpoint = json.loads(args.source.read_text(encoding="utf-8"))
    calls = next(
        message["tool_calls"]
        for message in reversed(checkpoint["values"]["messages"])
        if message.get("tool_calls")
        and all(
            call["name"] == "project_create_artifact" for call in message["tool_calls"]
        )
    )
    if len(calls) < 2:
        raise RuntimeError("源记录不是并行制品批次")
    identity = (
        datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
        + "-"
        + uuid4().hex[:6]
    )
    directory = ROOT / "workspace/artifact-replays" / identity
    recorder = TrajectoryRecorder(directory)
    store = InMemoryStore()
    workflow = ProjectWorkflow(store)
    snapshot = ProjectSnapshot(
        "artifact-replay-" + identity, "隔离验证原始制品批次", (), ()
    )
    await ProjectStore(store).save(snapshot)
    model = ReplayModel(
        responses=[
            AIMessage(content="", tool_calls=calls),
            AIMessage(content="REPLAY_COMPLETE"),
        ]
    )
    graph = create_agent(
        model,
        middleware=[
            TrajectoryMiddleware(recorder, stop_on_error=True),
            ProjectStateMiddleware(workflow=workflow),
        ],
        store=store,
    )
    report = {
        "source": str(args.source.resolve()),
        "scope": "exact recorded tool calls; deterministic model; real tools; ephemeral InMemoryStore",
        "source_call_hash": sha256(
            json.dumps(calls, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "call_count": len(calls),
        "status": "running",
    }
    started = time.monotonic()
    try:
        result = await graph.ainvoke(
            {"messages": [("human", "隔离重放已有工具调用，不运行网站开发")]},
            {
                "configurable": {
                    "thread_id": identity,
                    "project_id": snapshot.project_id,
                    "project_state_single_writer": True,
                }
            },
        )
        outputs = {
            message.tool_call_id: message
            for message in result["messages"]
            if isinstance(message, ToolMessage)
        }
        references = []
        for call in calls:
            message = outputs[call["id"]]
            if message.status != "success":
                raise AssertionError(message.content)
            reference = json.loads(message.content)["reference"]
            artifact = await workflow.artifacts.get(
                snapshot.project_id, reference, task_id=call["args"]["task_id"]
            )
            if artifact["content"] != call["args"]["content"]:
                raise AssertionError("制品读回正文与原请求不一致")
            references.append(reference)
        assert await ProjectStore(store).load(snapshot.project_id) == snapshot
        assert not (directory / "first-error.json").exists()
        assert result["messages"][-1].content == "REPLAY_COMPLETE"
        report.update(
            status="passed",
            successful_calls=len(outputs),
            references=references,
            snapshot_unchanged=True,
            first_error=False,
        )
    except Exception as error:
        report.update(
            status="failed", error={"type": type(error).__name__, "message": str(error)}
        )
        raise
    finally:
        report["seconds"] = time.monotonic() - started
        (directory / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=True), flush=True)
        print(str(directory / "report.json"), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
