"""首错收尾取消与Run自然结束竞态，不能把未知404当作已停止。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import run_pm_project_hub as observer
from run_pm_project_hub import cancel_observed_run


def test_official_human_interrupt_is_retained_as_pending_host_request():
    request = {
        "id": "decision",
        "value": {"action_requests": [{"name": "request_human_decision"}]},
    }
    assert observer.pending_host_requests({"tasks": [{"interrupts": [request]}]}) == [
        request
    ]
    assert observer.pending_host_requests(
        {"interrupts": [request], "tasks": [{"interrupts": [request]}]}
    ) == [request]
    assert observer.pending_host_requests({"tasks": []}) == []


def test_resume_hitl_requires_explicit_response_and_does_not_approve():
    request = {
        "id": "decision",
        "value": {"action_requests": [{"name": "request_human_decision"}]},
    }
    checkpoint = {"tasks": [{"interrupts": [request]}]}
    with pytest.raises(ValueError):
        observer.host_resume_command(checkpoint)
    command = observer.host_resume_command(checkpoint, "继续，未决定验收通过")
    assert command == {
        "resume": {
            "decisions": [{"type": "respond", "message": "继续，未决定验收通过"}]
        }
    }
    assert observer.host_resume_command({"tasks": []}) is None
    with pytest.raises(ValueError):
        observer.host_resume_command({"tasks": []}, "不能投递错检查点")


@pytest.mark.parametrize("code", [404, 409])
@pytest.mark.parametrize("status", ["error", "running"])
def test_cancel_race_requires_official_terminal_confirmation(code, status):
    async def scenario():
        response = httpx.Response(
            code, request=httpx.Request("POST", "http://server/cancel")
        )
        error = httpx.HTTPStatusError(
            "cancel race", request=response.request, response=response
        )
        client = SimpleNamespace(
            runs=SimpleNamespace(
                cancel=AsyncMock(side_effect=error),
                get=AsyncMock(return_value={"status": status}),
            )
        )
        if status == "running":
            with pytest.raises(httpx.HTTPStatusError):
                await cancel_observed_run(client, "thread", "run")
        else:
            result = await cancel_observed_run(client, "thread", "run")
            assert result["status"] == "error" and not result["cancel_requested"]
        client.runs.get.assert_awaited_once_with("thread", "run")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "provider,stopped", [("claude", False), ("codex", False), ("strict-provider", True)]
)
@pytest.mark.parametrize("failure", ["failed_status", "exit_code"])
def test_native_tool_failure_respects_developer_recovery_scope(
    tmp_path, monkeypatch, provider, stopped, failure
):
    """两种Worker的failed/非零退出仍留红色事件，但不终止整场测试。"""

    async def scenario():
        monkeypatch.setenv("PROJECT_HUB_RUN_DIRECTORY", str(tmp_path))
        monkeypatch.setattr(observer, "_trajectory", None)
        monkeypatch.setattr(
            observer, "ObservedWorker", lambda *a, **kw: SimpleNamespace(**kw)
        )
        payload = {
            "sessionId": "native",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call",
                "status": "failed",
            },
        }
        if failure == "exit_code":
            payload["update"].pop("status")
            payload["update"]["_meta"] = {"terminal_exit": {"exit_code": 1}}
        event = SimpleNamespace(
            update=SimpleNamespace(session_update="tool_call_update"),
            model_dump=lambda **kw: payload,
        )
        await observer.instrumented_worker(provider).on_update(event)
        assert (tmp_path / "first-error.json").exists() is stopped
        assert '"phase": "error"' in (tmp_path / "trajectory.jsonl").read_text(
            encoding="utf-8"
        )
        assert (tmp_path / "native-events.jsonl").exists()

    asyncio.run(scenario())


def test_resume_does_not_reclassify_historical_failure_as_new_failure():
    runs = {
        "worker": [
            {"run_id": "old-error", "status": "error"},
            {"run_id": "new-error", "status": "error"},
            {"run_id": "active", "status": "running"},
        ]
    }
    assert observer.failed_worker_runs(runs, {"old-error"}) == ["new-error"]


def test_resume_native_replay_is_logged_but_new_failure_still_stops(
    tmp_path, monkeypatch
):
    """以原生会话和工具ID识别已留证失败，不放过后续新工具错误。"""

    async def scenario():
        source = tmp_path / "workspace/project-hub-runs/old"
        source.mkdir(parents=True)
        output = tmp_path / "current"
        output.mkdir()
        payload = {
            "sessionId": "session",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "old-call",
                "status": "failed",
            },
        }
        (source / "native-events.jsonl").write_text(
            json.dumps({"provider": "strict-provider", "event": payload}) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(observer, "ROOT", tmp_path)
        monkeypatch.setenv("PROJECT_HUB_RESUME_RUN_DIRECTORY", str(source))
        monkeypatch.setenv("PROJECT_HUB_RUN_DIRECTORY", str(output))
        monkeypatch.setattr(observer, "_trajectory", None)
        monkeypatch.setattr(
            observer, "ObservedWorker", lambda *a, **kw: SimpleNamespace(**kw)
        )
        worker = observer.instrumented_worker("strict-provider")
        event = SimpleNamespace(
            update=SimpleNamespace(session_update="tool_call"),
            model_dump=lambda **kw: payload,
        )
        await worker.on_update(event)
        assert not (output / "first-error.json").exists()
        saved = json.loads(
            (output / "native-events.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert saved["historical_replay"] is True
        payload["update"]["toolCallId"] = "new-call"
        await worker.on_update(event)
        assert (output / "first-error.json").exists()

    asyncio.run(scenario())
