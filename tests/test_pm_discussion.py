"""A Clef decision is bound to actual task inputs and Worker provenance."""

import asyncio
from dataclasses import replace

import httpx
import pytest
from langgraph.store.memory import InMemoryStore

from src.deep_agents.agents.pm_agent.discussion import DiscussionGate
from src.middlewares.execution.security_policy import (
    EnforcementCapability,
    EnforcementFact,
)
from src.middlewares.project_state.folders import ProjectFolders
from src.middlewares.project_state.models import ProjectSnapshot, Task, TaskStatus
from src.middlewares.project_state.persistence import ProjectStore


def test_decision_uses_registered_result_and_expires_on_input_change(tmp_path):
    async def scenario():
        store = InMemoryStore()
        gate = DiscussionGate(store)
        await ProjectFolders(store).bind("project", str(tmp_path))
        brief = await gate.workflow.artifacts.create("project", "task", "task_brief", "# Goal\nDeliver the feature")
        task = Task("task", "Feature", TaskStatus.READY, "developer", frozenset(), frozenset(), ("Feature works",), artifact_refs=(brief["reference"],))
        await ProjectStore(store).save(ProjectSnapshot("project", "Goal", (), (task,)))
        enforcement = EnforcementFact("test_sandbox", EnforcementCapability.ENFORCEABLE, frozenset({"worker.readonly"}), mechanism="test fixture")
        await gate.workflow.record_start("project", "task", "parent", "worker", "run", phase="discussion", readonly_enforcement=enforcement)
        assert (await ProjectStore(store).load("project")).tasks[0].status is TaskStatus.READY
        result_ref = await gate.workflow.record_result("project", "parent", {"agent_id": "worker", "run_id": "run", "status": "success", "result": "I agree to the proposed scope."})

        async def handler(request):
            return httpx.Response(200, json={"success": True, "result": {"answers": {"development_readiness": {"choice": "pass"}}}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            decision = await gate.evaluate("project", "task", owner_thread="parent", worker_result_ref=result_ref, pm_confirmation="I agree to this scope", account_id="abc123", token="secret", client=client)
        assert decision["decision"] == "pass"
        assert (await gate.require_pass("project", "task", worker_agent_id="worker"))["decision_id"] == decision["decision_id"]
        with pytest.raises(ValueError, match="discussion_required"):
            await gate.require_pass("project", "task", worker_agent_id="another")
        snapshot = await ProjectStore(store).load("project")
        await ProjectStore(store).save(replace(snapshot, goal="Changed goal"))
        with pytest.raises(ValueError, match="discussion_required"):
            await gate.require_pass("project", "task", worker_agent_id="worker")

    asyncio.run(scenario())


def test_pending_dispatch_blocks_retry_until_run_is_bound():
    async def scenario():
        from unittest.mock import patch
        from types import SimpleNamespace

        store = InMemoryStore()
        workflow = ProjectWorkflow(store)
        brief = await workflow.artifacts.create("p", "t", "task_brief", "# Task\nImplement")
        task = Task("t", "Task", TaskStatus.READY, "developer", frozenset(), frozenset(), ("Done",), artifact_refs=(brief["reference"],))
        await ProjectStore(store).save(ProjectSnapshot("p", "Goal", (), (task,)))
        args = {"owner_thread": "owner", "operation": "followup_task", "decision_id": "decision", "worker_agent_id": "worker", "message_digest": "authorized-message"}
        intent = await workflow.begin_dispatch("p", "t", **args)
        with pytest.raises(ValueError, match="上次派工结果未确认"):
            await workflow.begin_dispatch("p", "t", **args)
        with pytest.raises(ValueError, match="尚未关联"):
            await workflow.complete_dispatch("p", "t", agent_id="worker", run_id="run")
        await workflow.record_start("p", "t", "owner", "worker", "run", dispatch_id=intent["intent_id"])
        await workflow.complete_dispatch("p", "t", agent_id="worker", run_id="run")
        async def get_run(*args):
            return {"status": "success"}

        with patch("src.middlewares.project_state.workflow.get_client", return_value=SimpleNamespace(runs=SimpleNamespace(get=get_run))):
            await workflow.begin_dispatch("p", "t", **args)

    from src.middlewares.project_state.workflow import ProjectWorkflow

    asyncio.run(scenario())


@pytest.mark.parametrize("change_during_connect", [False, True])
def test_readonly_intent_binds_message_run_and_current_input(tmp_path, monkeypatch, change_during_connect):
    """Changed messages, replayed Runs and stale task inputs cannot reach a Worker."""
    import json
    from types import SimpleNamespace

    from langchain_core.messages import HumanMessage

    from src.middlewares.acp import graph as acp_graph
    from src.middlewares.acp.correlation import WorkerRef
    from src.middlewares.project_state.artifacts import content_hash
    from src.middlewares.project_state.workflow import ProjectWorkflow
    from src.runtime.routing.models import WorkerCandidate

    async def scenario():
        folder = tmp_path / "project"
        folder.mkdir()
        store = InMemoryStore()
        workflow = ProjectWorkflow(store)
        brief = await workflow.artifacts.create("p", "t", "task_brief", "brief")
        task = Task("t", "Task", TaskStatus.READY, "developer", frozenset(), frozenset(), ("done",), artifact_refs=(brief["reference"],))
        await ProjectStore(store).save(ProjectSnapshot("p", "goal", (), (task,)))
        await ProjectFolders(store).bind("p", str(folder))
        enforcement = EnforcementFact("wsl", EnforcementCapability.ENFORCEABLE, frozenset({"worker.readonly"}), mechanism="test")
        intent = await workflow.begin_dispatch(
            "p", "t", owner_thread="owner", operation="spawn_agent", phase="discussion",
            readonly_enforcement=enforcement, message_digest=content_hash("authorized"), discussion_question="question",
        )
        calls = []

        class Worker:
            process = SimpleNamespace(returncode=None)
            cwd = str(folder)
            readonly_enforcement = enforcement

            async def new(self, **identity):
                return WorkerRef("developer", "native", identity["project_id"], identity["task_id"], identity["thread_id"], identity["role"], self.cwd, isolation="wsl-readonly")

            async def control(self, ref, action, payload, *, on_update):
                calls.append(payload["text"])
                return SimpleNamespace(stop_reason="end_turn")

        worker = Worker()

        class Pool:
            count = 0

            async def get_readonly(self, *args):
                self.count += 1
                if change_during_connect and self.count == 2:
                    await ProjectStore(store).save(ProjectSnapshot("p", "changed", (), (task,)))
                return worker

        monkeypatch.setattr(acp_graph, "get_store", lambda: store)
        candidate = WorkerCandidate("developer", "acp", frozenset({"developer"}), frozenset(), True)
        graph = acp_graph.create_worker_graph({"developer": worker}, [candidate], project_workers=Pool())
        payload = {"operation": "spawn", "owner_thread_id": "owner", "project_id": "p", "task_id": "t", "agent_type": "developer", "message": "altered", "pm_governed": True, "pm_phase": "discussion", "pm_dispatch_id": intent["intent_id"], "pm_provider_id": "developer"}
        config = {"configurable": {"thread_id": "worker"}, "metadata": {"run_id": "run-one"}}
        with pytest.raises(PermissionError):
            await graph.ainvoke({"messages": [HumanMessage(content=json.dumps(payload))]}, config)
        assert not calls
        payload["message"] = "authorized"
        if change_during_connect:
            with pytest.raises(PermissionError):
                await graph.ainvoke({"messages": [HumanMessage(content=json.dumps(payload))]}, config)
            assert not calls
        else:
            await graph.ainvoke({"messages": [HumanMessage(content=json.dumps(payload))]}, config)
            assert calls == ["authorized"]
            with pytest.raises(PermissionError):
                await graph.ainvoke({"messages": [HumanMessage(content=json.dumps(payload))]}, {"configurable": {"thread_id": "worker"}, "metadata": {"run_id": "run-two"}})
            assert calls == ["authorized"]

    asyncio.run(scenario())


@pytest.mark.parametrize("role", ["developer", "reviewer"])
def test_pm_spawn_is_rejected_before_worker_run(role):
    import json
    from unittest.mock import patch

    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, ToolMessage
    from test_acp_middleware import client_fixture
    from test_pm_memory_integration import ToolModel

    from src.middlewares.acp import ACPMiddleware
    from src.middlewares.project_state.middleware import ProjectStateMiddleware
    from src.middlewares.project_state.workflow import ProjectWorkflow

    async def scenario():
        store = InMemoryStore()
        workflow = ProjectWorkflow(store)
        brief = await workflow.artifacts.create("p", "t", "task_brief", "# Task\nImplement")
        task = Task("t", "Task", TaskStatus.READY, role, frozenset(), frozenset(), ("Done",), artifact_refs=(brief["reference"],))
        await ProjectStore(store).save(ProjectSnapshot("p", "Goal", (), (task,)))
        client = client_fixture()
        model = ToolModel(responses=[AIMessage(content="", tool_calls=[{"name": "spawn_agent", "args": {"task_name": "t", "message": "Implement"}, "id": "call"}]), AIMessage(content="blocked")])
        graph = create_agent(model, middleware=[ProjectStateMiddleware(workflow=workflow, pm_discussion_gate=True), ACPMiddleware(async_subagents=[{"name": role, "description": role, "graph_id": "acp_worker"}])], store=store)
        with patch("deepagents.middleware.async_subagents.get_client", return_value=client):
            result = await graph.ainvoke({"messages": [("user", "start")]}, {"configurable": {"project_id": "p", "thread_id": "parent", "project_state_single_writer": True}})
        message = next(message for message in result["messages"] if isinstance(message, ToolMessage))
        assert json.loads(message.content)["error"]["code"] == "discussion_required"
        assert (await ProjectStore(store).load("p")).tasks[0].status is TaskStatus.READY
        client.runs.create.assert_not_called()

    asyncio.run(scenario())
