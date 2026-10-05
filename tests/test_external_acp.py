"""Protocol integration tests with SDK-spawned, model-free ACP processes."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("acp")

from src.middlewares.approval.decision import ApprovalDecision, RiskLevel
from src.middlewares.execution.security_policy import SecurityGrant
from src.runtime.external_agents import (
    ACPWorker,
    UnsupportedControl,
    WorkerRef,
    worker_candidate,
)
from src.runtime.routing import RoleRequirement, route_worker

FIXTURE = Path(__file__).parent / "fixtures" / "acp_agent.py"
CONTROL_GRANTS = frozenset(
    {
        "worker.connect",
        "worker.new",
        "worker.observe",
        "worker.ask",
        "worker.cancel",
        "worker.resume",
        "worker.list",
        "worker.close",
        "worker.fork",
        "worker.set_mode",
        "worker.set_config",
        "worker.authenticate",
    }
)


class Policy:
    name = "test-policy"

    def __init__(self, decision=None):
        self.decision = decision or ApprovalDecision.allowed()
        self.requests = []

    async def aevaluate(self, request):
        self.requests.append(request)
        return self.decision


def worker(tmp_path, profile="full", **kwargs):
    grants = kwargs.pop(
        "grants", lambda _ref: SecurityGrant(CONTROL_GRANTS | {"external_tool.read"})
    )
    return ACPWorker(
        profile,
        [sys.executable, str(FIXTURE), profile, str(tmp_path / f"{profile}.json")],
        tmp_path,
        grants=grants,
        timeout=5,
        **kwargs,
    )


async def new(instance):
    return await instance.new(
        project_id="project", task_id="task", thread_id="thread", role="developer"
    )


@pytest.mark.parametrize("profile", ["full", "basic"])
def test_same_control_interface_with_two_different_provider_capabilities(
    tmp_path, profile
):
    async def scenario():
        updates = []

        async def capture(notification):
            updates.append(notification)

        async with worker(tmp_path, profile, on_update=capture) as instance:
            ref = await new(instance)
            response = await instance.control(ref, "ask", {"text": "Hello"})
            assert response.stop_reason == "end_turn"
            assert updates[-1].update.content.text == "Hello"
            assert updates[-1].field_meta == {"fixture.marker": "preserved"}
            observation = instance.observe(ref)
            assert observation["worker_ref"]["task_id"] == "task"
            assert observation["compaction_visibility"] == "unknown"
            assert ("resume" in observation["supported_controls"]) == (
                profile == "full"
            )
            with pytest.raises(UnsupportedControl):
                await instance.control(ref, "steer", {"text": "change plan"})
        assert instance.process.returncode is not None

    asyncio.run(scenario())


def test_native_session_load_after_connection_restart_preserves_history(tmp_path):
    async def scenario():
        async with worker(tmp_path) as instance:
            ref = await new(instance)
            await instance.control(ref, "ask", {"text": "Remember original decision"})
        updates = []

        async def capture(notification):
            updates.append(notification)

        async with worker(tmp_path, on_update=capture) as restarted:
            await restarted.resume(WorkerRef(**ref.to_dict()))
            await restarted.control(ref, "ask", {"text": "history"})
            assert "Remember original decision" in updates[-1].update.content.text
            assert ref.session_id in {
                session.session_id
                for session in (await restarted.list_sessions()).sessions
            }

    asyncio.run(scenario())


def test_permission_requires_current_grant_and_policy_then_selects_allow_once(tmp_path):
    async def scenario():
        policy = Policy()
        updates = []

        async def capture(notification):
            updates.append(notification)

        async with worker(tmp_path, approval=policy, on_update=capture) as instance:
            ref = await new(instance)
            await instance.control(ref, "ask", {"text": "permission:read"})
            assert json.loads(updates[-1].update.content.text) == {
                "optionId": "once",
                "outcome": "selected",
            }
            assert (
                policy.requests[-1].context["worker_ref"]["session_id"]
                == ref.session_id
            )
            assert policy.requests[-1].tool_input == {"path": "sample.txt"}
            await instance.control(ref, "ask", {"text": "permission:execute"})
            assert json.loads(updates[-1].update.content.text) == {
                "outcome": "cancelled"
            }
            await instance.control(ref, "ask", {"text": "permission:always"})
            assert json.loads(updates[-1].update.content.text) == {
                "outcome": "cancelled"
            }

    asyncio.run(scenario())


def test_human_approval_rechecks_revoked_grants(tmp_path):
    async def scenario():
        allowed = set(CONTROL_GRANTS | {"external_tool.read"})

        async def review(request, decision):
            allowed.remove("external_tool.read")
            return True

        updates = []

        async def capture(notification):
            updates.append(notification)

        policy = Policy(ApprovalDecision.needs_approval(RiskLevel.MEDIUM, "Review"))
        async with worker(
            tmp_path,
            grants=lambda _ref: SecurityGrant(frozenset(allowed)),
            approval=policy,
            review=review,
            on_update=capture,
        ) as instance:
            ref = await new(instance)
            await instance.control(ref, "ask", {"text": "permission:read"})
            assert json.loads(updates[-1].update.content.text)["outcome"] == "cancelled"

    asyncio.run(scenario())


def test_permission_callback_failure_is_denied(tmp_path):
    async def scenario():
        class BrokenPolicy:
            async def aevaluate(self, request):
                raise RuntimeError("policy unavailable")

        updates = []

        async def capture(notification):
            updates.append(notification)

        async with worker(
            tmp_path, approval=BrokenPolicy(), on_update=capture
        ) as instance:
            ref = await new(instance)
            await instance.control(ref, "ask", {"text": "permission:read"})
            assert json.loads(updates[-1].update.content.text)["outcome"] == "cancelled"

    asyncio.run(scenario())


def test_cancel_running_prompt_without_starting_second_turn(tmp_path):
    async def scenario():
        started = asyncio.Event()

        async def capture(notification):
            if notification.update.content.text == "started":
                started.set()

        async with worker(tmp_path, on_update=capture) as instance:
            ref = await new(instance)
            prompt = asyncio.create_task(
                instance.control(ref, "ask", {"text": "block"})
            )
            await asyncio.wait_for(started.wait(), timeout=3)
            with pytest.raises(RuntimeError, match="already in flight"):
                await instance.control(ref, "ask", {"text": "Another turn"})
            await instance.control(ref, "cancel")
            assert (await prompt).stop_reason == "cancelled"

    asyncio.run(scenario())


def test_timeout_cancels_turn_and_connection_cleanup_is_sdk_owned(tmp_path):
    async def scenario():
        instance = worker(tmp_path)
        async with instance:
            ref = await new(instance)
            instance.timeout = 0.15
            with pytest.raises(TimeoutError):
                await instance.control(ref, "ask", {"text": "block"})
        assert instance.connection is None
        assert instance.process.returncode is not None

    asyncio.run(scenario())


def test_native_modes_config_fork_and_close(tmp_path):
    async def scenario():
        async with worker(tmp_path) as instance:
            ref = await new(instance)
            await instance.control(ref, "set_mode", {"mode_id": "edit"})
            assert instance.observe(ref)["session"]["modes"]["currentModeId"] == "edit"
            with pytest.raises(UnsupportedControl):
                await instance.control(ref, "set_mode", {"mode_id": "invented"})
            await instance.control(
                ref, "set_config", {"config_id": "model", "value": "large"}
            )
            assert (
                instance.observe(ref)["session"]["configOptions"][0]["currentValue"]
                == "large"
            )
            child = await instance.control(ref, "fork", {"task_id": "child-task"})
            assert child.session_id != ref.session_id and child.task_id == "child-task"
            await instance.control(child, "close")
            with pytest.raises(ValueError):
                instance.observe(child)

    asyncio.run(scenario())


def test_control_grants_are_rechecked_and_provider_mismatch_rejected(tmp_path):
    async def scenario():
        allowed = set(CONTROL_GRANTS)
        async with worker(
            tmp_path, grants=lambda _ref: SecurityGrant(frozenset(allowed))
        ) as instance:
            ref = await new(instance)
            allowed.remove("worker.ask")
            with pytest.raises(PermissionError):
                await instance.control(ref, "ask", {"text": "No grant"})
            allowed.remove("worker.observe")
            await instance.control(ref, "cancel")
            wrong = WorkerRef(**{**ref.to_dict(), "provider_id": "another"})
            with pytest.raises(ValueError):
                await instance.resume(wrong)

    asyncio.run(scenario())


def test_router_uses_negotiated_controls_without_granting_permission(tmp_path):
    async def scenario():
        candidates = []
        for profile in ("full", "basic"):
            async with worker(tmp_path, profile) as instance:
                candidates.append(
                    worker_candidate(
                        profile,
                        frozenset({"developer"}),
                        instance.initialized,
                        business_capabilities=frozenset({"python"}),
                    )
                )
        decision = route_worker(
            RoleRequirement(
                role="developer",
                required_capabilities=frozenset({"python", "acp.resume"}),
            ),
            candidates,
        )
        assert decision.provider_id == "full"
        assert "external_tool.execute" not in candidates[0].reported_capabilities

    asyncio.run(scenario())


def test_cli_json_stream_and_native_resume_using_saved_domain_ref(tmp_path):
    command = json.dumps(
        [
            sys.executable,
            str(FIXTURE.resolve()),
            "full",
            str(tmp_path / "provider.json"),
        ]
    )
    ref_file = tmp_path / "domain-ref.json"
    base = [
        sys.executable,
        "-m",
        "src.runtime.external_agents",
        "--provider",
        "full",
        "--command-json",
        command,
        "--cwd",
        str(tmp_path),
        "--json",
    ]
    first = subprocess.run(
        [*base, "--prompt", "Remember CLI decision", "--ref-out", str(ref_file)],
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert first.returncode == 0, first.stderr
    messages = [json.loads(line) for line in first.stdout.splitlines()]
    assert messages[-1]["stopReason"] == "end_turn"
    assert messages[0]["method"] == "session/update"
    ref = json.loads(ref_file.read_text())
    assert set(ref) == {
        "provider_id",
        "session_id",
        "project_id",
        "task_id",
        "thread_id",
        "role",
        "cwd",
        "handoff_id",
    }
    second = subprocess.run(
        [*base, "--prompt", "history", "--ref-in", str(ref_file)],
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert second.returncode == 0, second.stderr
    assert (
        "Remember CLI decision"
        in json.loads(second.stdout.splitlines()[0])["params"]["update"]["content"][
            "text"
        ]
    )
