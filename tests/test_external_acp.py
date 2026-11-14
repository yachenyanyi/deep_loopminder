"""Protocol integration tests with SDK-spawned, model-free ACP processes."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("acp")

from src.middlewares.acp import (
    ACPWorker,
    UnsupportedControl,
    WorkerRef,
    worker_candidate,
)
from src.middlewares.approval.decision import ApprovalDecision, RiskLevel
from src.middlewares.execution.security_policy import SecurityGrant
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


def test_context_usage_is_only_taken_from_native_usage_update(tmp_path):
    from acp.schema import Cost, UsageUpdate

    async def scenario():
        async with worker(tmp_path) as instance:
            ref = await new(instance)
            assert instance.observe(ref)["context_usage"] is None
            instance._update_metadata(ref.session_id, UsageUpdate(sessionUpdate="usage_update", used=300, size=1000))
            observed = instance.observe(ref)
            assert observed["context_usage"]["remaining"] == 700
            assert observed["context_usage"]["source"] == "acp.session_update.usage_update"
            assert observed["compaction_visibility"] == "unknown"
            # 适配器未上报 cost 时保持缺失，不猜。
            assert "cost" not in observed["context_usage"]
            # 同一协议事件的 cost 字段可靠，上报时随用量一起记录。
            cost = Cost(currency="USD", amount=0.25)
            instance._update_metadata(
                ref.session_id,
                UsageUpdate(sessionUpdate="usage_update", used=500, size=1000, cost=cost),
            )
            usage = instance.observe(ref)["context_usage"]
            assert usage["remaining"] == 500
            assert usage["cost"] == {"amount": 0.25, "currency": "USD"}

    asyncio.run(scenario())


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


def test_live_worker_permission_can_be_approved_from_pm_workbench(tmp_path):
    """Exercise ACP subprocess, host policy, HTTP approval and protocol reply."""
    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    from src.workbench.worker_reviews import (
        WorkerReview,
        pending_worker_reviews,
        submit_worker_review,
    )

    async def scenario():
        async def owner(_ref):
            return "conversation"

        registry = WorkerReview(timeout=3, owner_for_worker=owner)
        app = Starlette(routes=[Route("/reviews", pending_worker_reviews, methods=["GET"]),
                                Route("/reviews", submit_worker_review, methods=["POST"])])
        app.state.worker_review = registry
        updates = []

        async def capture(notification):
            updates.append(notification)

        policy = Policy(ApprovalDecision.needs_approval(RiskLevel.MEDIUM, "Review"))
        async with worker(tmp_path, approval=policy, review=registry.review, on_update=capture) as instance:
            ref = await new(instance)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
                                         base_url="http://127.0.0.1") as client:
                turn = asyncio.create_task(instance.control(ref, "ask", {"text": "permission:read"}))
                async with asyncio.timeout(3):
                    while not registry.pending("project", "conversation"):
                        await asyncio.sleep(0.01)
                request_id = (await client.get("/reviews?project_id=project&thread_id=conversation")).json()["requests"][0]["request_id"]
                result = await client.post("/reviews", json={"project_id": "project", "thread_id": "conversation", "request_id": request_id,
                                                              "approved": True})
                assert result.status_code == 200
                await turn
            assert json.loads(updates[-1].update.content.text) == {"optionId": "once", "outcome": "selected"}
            assert len(policy.requests) == 2

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


def test_unlimited_prompt_can_still_be_cancelled(tmp_path):
    async def scenario():
        started = asyncio.Event()

        async def capture(notification):
            if notification.update.content.text == "started":
                started.set()

        instance = worker(tmp_path, on_update=capture)
        instance.timeout = None
        async with instance:
            ref = await new(instance)
            prompt = asyncio.create_task(instance.control(ref, "ask", {"text": "block"}))
            await asyncio.wait_for(started.wait(), timeout=3)
            await instance.control(ref, "cancel")
            assert (await prompt).stop_reason == "cancelled"
        assert instance.process.returncode is not None

    asyncio.run(scenario())


def test_native_modes_config_fork_and_close(tmp_path):
    async def scenario():
        async with worker(tmp_path) as instance:
            ref = await new(instance)
            # A bare set_mode response carries no mode, so local state must not
            # claim the switch happened.
            await instance.control(ref, "set_mode", {"mode_id": "edit"})
            assert instance.observe(ref)["session"]["configOptions"][0]["currentValue"] == "safe"
            applied = await instance.apply_mode(ref, "edit")
            assert applied["reported_mode"] == "edit" and applied["matched"] is True
            assert instance.observe(ref)["session"]["configOptions"][0]["currentValue"] == "edit"
            with pytest.raises(UnsupportedControl):
                await instance.apply_mode(ref, "invented")
            await instance.control(
                ref, "set_config", {"config_id": "model", "value": "large"}
            )
            options = instance.observe(ref)["session"]["configOptions"]
            assert next(o for o in options if o["id"] == "model")["currentValue"] == "large"
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
        "src.middlewares.acp",
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
        "isolation",
        "config_version",
    }
    # References written before isolation metadata existed remain resumable.
    ref.pop("isolation")
    ref.pop("config_version")
    ref_file.write_text(json.dumps(ref), encoding="utf-8")
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


def test_pm_execution_reconciles_only_matching_verified_job(tmp_path, monkeypatch):
    """Persist ownership before spawn and retain unproven shutdowns after restart."""
    from langgraph.store.memory import InMemoryStore

    from src.deep_agents.agents.pm_agent.collaboration import (
        PMWorkerGovernance,
        reconcile_verified_jobs,
        verified_job_stop,
    )
    from src.middlewares.project_state.folders import ProjectFolders

    async def scenario():
        monkeypatch.setenv("LOOPMINDER_DATA_DIR", str(tmp_path))
        store = InMemoryStore()
        governance = PMWorkerGovernance(lambda: store)
        checks = []

        async def authorize(request, ref, config):
            checks.append((request["owner_thread_id"], ref.thread_id, config["metadata"]["run_id"]))

        async def claim(project_id, task_id, intent_id, run_id):
            checks.append((project_id, task_id, intent_id, run_id))

        async def digest(_self, _project_id):
            return "test"

        monkeypatch.setattr(governance, "authorize_execution", authorize)
        monkeypatch.setattr(governance, "claim_intent", claim)
        monkeypatch.setattr(ProjectFolders, "content_digest", digest)
        ref = WorkerRef("claude", "native", "project", "task", "worker", "developer", str(tmp_path))
        request = {"owner_thread_id": "conversation", "pm_dispatch_id": "intent"}
        config = {"metadata": {"run_id": "official-run"}}
        record = await governance.begin_execution(ref, request, config, {"intent_id": "intent"})
        namespace = ("deep_loopminder", "project_worker_quarantine", "project")
        assert checks == [("conversation", "worker", "official-run"), ("project", "task", "intent", "official-run")]
        assert (await store.aget(namespace, "worker")).value == record
        assert record["owner_thread_id"] == "conversation" and record["official_run_id"] == "official-run"
        status = Path(record["job_status"])
        assert not status.exists()
        await reconcile_verified_jobs(store, "project")
        assert await store.aget(namespace, "worker") is not None
        status.write_text(json.dumps({"state": "stopped_verified", "execution_id": "different"}), encoding="utf-8")
        assert not verified_job_stop(record)
        await reconcile_verified_jobs(store, "project")
        assert await store.aget(namespace, "worker") is not None
        status.write_text(json.dumps({"state": "stopped_verified", "execution_id": record["execution_id"]}), encoding="utf-8")
        await reconcile_verified_jobs(store, "project")
        assert await store.aget(namespace, "worker") is None
        await store.aput(namespace, "old-worker", {"project_id": "project", "status": "shutdown_unconfirmed"})
        await reconcile_verified_jobs(store, "project")
        assert await store.aget(namespace, "old-worker") is not None

    asyncio.run(scenario())


def test_discussion_can_coexist_with_other_execution_but_missing_run_is_unknown(tmp_path, monkeypatch):
    """Allow another task's discussion without assuming a missing Run stopped."""
    from types import SimpleNamespace

    from langgraph.store.memory import InMemoryStore

    from src.middlewares.execution.security_policy import (
        EnforcementCapability,
        EnforcementFact,
    )
    from src.middlewares.project_state import workflow as workflow_module
    from src.middlewares.project_state.transitions import ProjectStateError

    async def scenario():
        store = InMemoryStore()
        workflow = workflow_module.ProjectWorkflow(store)

        async def task(_project_id, _task_id):
            return object(), object()

        async def version(_snapshot, _task):
            return "version"

        monkeypatch.setattr(workflow, "task", task)
        monkeypatch.setattr(workflow, "input_version", version)
        fact = EnforcementFact(surface="test", capability=EnforcementCapability.PARTIAL,
                               enforced_capabilities=frozenset(), mechanism="test declaration", gap="test")
        await store.aput(("deep_loopminder", "project_worker_quarantine", "project"), "worker-a",
                         {"project_id": "project", "task_id": "task-a"})
        common = dict(owner_thread="conversation-b", operation="spawn_agent", phase="discussion",
                      readonly_enforcement=fact, discussion_question="question", message_digest="digest")
        result = await workflow.begin_dispatch("project", "task-b", **common)
        assert result["owner_thread"] == "conversation-b" and result["input_version"] == "version"
        with pytest.raises(ProjectStateError) as error:
            await workflow.begin_dispatch("project", "task-a", **common)
        assert error.value.details["code"] == "worker_shutdown_unknown"
        with pytest.raises(ProjectStateError) as error:
            await workflow.begin_dispatch("project", "task-c", owner_thread="conversation-c",
                                          operation="spawn_agent", phase="execution", decision_id="decision",
                                          message_digest="digest")
        assert error.value.details["code"] == "worker_shutdown_unknown"

        class MissingRun(Exception):
            response = SimpleNamespace(status_code=404)

        class Runs:
            async def get(self, *_):
                raise MissingRun()

        monkeypatch.setattr(workflow_module, "get_client", lambda **_: SimpleNamespace(runs=Runs()))
        namespace = ("deep_loopminder", "project_dispatch_intents", "project")
        await store.aput(namespace, "task-d", {"status": "confirmed", "agent_id": "worker-d", "run_id": "run-d"})
        with pytest.raises(ProjectStateError) as error:
            await workflow.begin_dispatch("project", "task-d", **common)
        assert error.value.details["code"] == "dispatch_status_unknown"
        assert (await store.aget(namespace, "task-d")).value["run_id"] == "run-d"

    asyncio.run(scenario())
