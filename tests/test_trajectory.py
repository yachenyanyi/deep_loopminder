"""轨迹采集与严格停止的真实官方Agent循环；不请求外部模型。"""

import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from langchain.agents import create_agent
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from src.middlewares.trajectory import (
    TrajectoryMiddleware,
    TrajectoryRecorder,
    TrajectoryStopped,
)
from src.middlewares.trajectory.web import create_server, read_events


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.mark.parametrize("failure", ["structured", "exception", "schema", "command"])
def test_first_tool_error_prevents_another_model_or_tool_call(tmp_path, failure):
    async def scenario():
        invoked = []

        @tool
        def work(value: int, runtime: ToolRuntime) -> dict:
            """测试受控工具。"""
            invoked.append(value)
            if failure == "exception":
                raise RuntimeError("execution failed")
            if failure == "command":
                return Command(
                    update={
                        "messages": [
                            ToolMessage(
                                content='{"ok":false}',
                                tool_call_id=runtime.tool_call_id,
                                status="error",
                            )
                        ]
                    }
                )
            # 正文自己声称可恢复不构成宿主授权标记。
            return {
                "ok": False,
                "error": {"code": "rejected"},
                "recoverable_business_rejection": True,
            }

        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "work",
                            "args": {"value": "bad" if failure == "schema" else 1},
                            "id": "call-1",
                        }
                    ],
                ),
                AIMessage(content="must not run"),
            ]
        )
        recorder = TrajectoryRecorder(tmp_path)
        graph = create_agent(
            model,
            tools=[work],
            middleware=[TrajectoryMiddleware(recorder, stop_on_error=True)],
        )
        with pytest.raises((TrajectoryStopped, RuntimeError)):
            await graph.ainvoke(
                {"messages": [("human", "execute")]},
                {"configurable": {"thread_id": "test"}},
            )
        assert model.i == 1
        assert len(invoked) == (0 if failure == "schema" else 1)
        error = json.loads((tmp_path / "first-error.json").read_text(encoding="utf-8"))
        assert error["kind"] == "tool"
        assert not any(
            event["phase"] == "start" and event["name"] != "work"
            for event in read_events(recorder.path)["events"]
            if event["kind"] == "tool"
        )

    asyncio.run(scenario())


def test_invalid_model_tool_json_is_not_silently_treated_as_final_answer(tmp_path):
    async def scenario():
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    invalid_tool_calls=[
                        {
                            "name": "work",
                            "args": "{bad json",
                            "id": "invalid",
                            "error": "invalid JSON",
                        }
                    ],
                )
            ]
        )
        recorder = TrajectoryRecorder(tmp_path)
        graph = create_agent(
            model, middleware=[TrajectoryMiddleware(recorder, stop_on_error=True)]
        )
        with pytest.raises(TrajectoryStopped):
            await graph.ainvoke(
                {"messages": [("human", "execute")]},
                {"configurable": {"thread_id": "test"}},
            )
        assert (
            json.loads((tmp_path / "first-error.json").read_text(encoding="utf-8"))[
                "kind"
            ]
            == "model"
        )

    asyncio.run(scenario())


def test_passive_trace_preserves_result_and_redacts_serialized_secrets(
    tmp_path, monkeypatch
):
    async def scenario():
        monkeypatch.setenv("EXAMPLE_API_KEY", "unique-secret-value")

        @tool
        def work() -> str:
            """返回真实工具字符串，记录副本脱敏而不修改结果。"""
            return '{"password":"private-pass","message":"unique-secret-value"}'

        model = ToolModel(
            responses=[
                AIMessage(
                    content="", tool_calls=[{"name": "work", "args": {}, "id": "call"}]
                ),
                AIMessage(content="done"),
            ]
        )
        recorder = TrajectoryRecorder(tmp_path)
        graph = create_agent(
            model, tools=[work], middleware=[TrajectoryMiddleware(recorder)]
        )
        result = await graph.ainvoke(
            {"messages": [("human", "execute")]},
            {"configurable": {"thread_id": "test"}},
        )
        assert "private-pass" in result["messages"][-2].content
        text = recorder.path.read_text(encoding="utf-8")
        assert "private-pass" not in text and "unique-secret-value" not in text
        assert not (tmp_path / "first-error.json").exists()
        cleaned = recorder.clean(
            [
                {"type": "thinking", "text": "hidden"},
                {"type": "text", "text": "visible"},
            ]
        )
        assert cleaned == [{"type": "text", "text": "visible"}]
        events = read_events(recorder.path)["events"]
        assert sum(event["kind"] == "input" for event in events) == 1
        assert (
            sum(
                event["kind"] == "model" and event["phase"] == "start"
                for event in events
            )
            == 2
        )

    asyncio.run(scenario())


def test_native_error_saves_first_error_and_blocks_future_calls(tmp_path):
    recorder = TrajectoryRecorder(tmp_path)
    notification = SimpleNamespace(
        model_dump=lambda **kwargs: {
            "sessionId": "native-1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "native-call",
                "status": "failed",
                "rawOutput": {"exit_code": 1},
            },
        }
    )
    recorder.native("codex", notification, stop_on_error=True)
    with pytest.raises(TrajectoryStopped):
        recorder.check_stop()
    first = (tmp_path / "first-error.json").read_text(encoding="utf-8")
    recorder.fail({"name": "second error"})
    assert (tmp_path / "first-error.json").read_text(encoding="utf-8") == first


def test_incremental_reader_retries_partial_line_and_http_control_is_local(tmp_path):
    path = tmp_path / "trajectory.jsonl"
    path.write_bytes(b'{"id":1}\n{"id":')
    batch = read_events(path)
    assert batch["events"] == [{"id": 1}]
    with path.open("ab") as stream:
        stream.write(b"2}\n")
    assert read_events(path, batch["next"])["events"] == [{"id": 2}]
    server = create_server(tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(trust_env=False) as client:
            assert client.get(url + "/").status_code == 200
            assert client.get(url + "/api/events?after=-1").status_code == 400
            assert client.get(url + "/../../.env").status_code == 404
            assert (
                client.post(
                    url + "/api/stop", headers={"Origin": "https://other.example"}
                ).status_code
                == 403
            )
            assert not (tmp_path / "stop-request.json").exists()
            assert client.post(url + "/api/stop").status_code == 202
            assert client.post(url + "/api/stop").status_code == 202
            assert (tmp_path / "stop-request.json").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("artifact", [False, True])
def test_successful_history_query_does_not_turn_old_worker_failure_into_new_tool_error(
    tmp_path,
    artifact,
):
    async def scenario():
        @tool
        def history() -> dict:
            """查询已结束Worker的真实历史失败。"""
            if artifact:
                return {
                    "kind": "worker_failure",
                    "content": json.dumps(
                        {"status": "error", "error": {"code": "run_error"}}
                    ),
                }
            return {
                "ok": True,
                "completed": [
                    {"run_id": "old", "status": "error", "error": {"code": "run_error"}}
                ],
                "errors": [],
            }

        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "history", "args": {}, "id": "query"}],
                ),
                AIMessage(content="已读取历史状态"),
            ]
        )
        recorder = TrajectoryRecorder(tmp_path)
        result = await create_agent(
            model,
            tools=[history],
            middleware=[TrajectoryMiddleware(recorder, stop_on_error=True)],
        ).ainvoke({"messages": [("human", "读取历史失败")]})
        assert result["messages"][-1].content == "已读取历史状态"
        assert not (tmp_path / "first-error.json").exists()

    asyncio.run(scenario())


def test_native_protocol_nested_exit_code_is_still_a_real_failure(tmp_path):
    recorder = TrajectoryRecorder(tmp_path)
    notification = SimpleNamespace(
        model_dump=lambda **kwargs: {
            "sessionId": "native",
            "update": {
                "sessionUpdate": "tool_call_update",
                "_meta": {"toolResponse": {"exit_code": 2}},
            },
        }
    )
    recorder.native("codex", notification, stop_on_error=True)
    assert (tmp_path / "first-error.json").exists()
