"""Conversation ownership across project workbench endpoints."""

import asyncio
import json
from dataclasses import dataclass, replace
from types import SimpleNamespace

import httpx
from langgraph.store.memory import InMemoryStore
from starlette.applications import Starlette
from starlette.routing import Route

from src.middlewares.project_state.models import ProjectSnapshot
from src.middlewares.project_state.persistence import ProjectStore
from src.workbench import threads
from src.workbench.claims import ClaimWatcher
from src.workbench.context import THREADS
from src.workbench.worker_reviews import _owner_for_worker


@dataclass
class StepRuntime:
    store: object
    config: dict
    state: dict


@dataclass
class StepRequest:
    runtime: StepRuntime
    state: dict
    messages: list | None = None
    system_message: object = None
    tool_call: dict | None = None
    tools: tuple = ()

    def override(self, **changes):
        return replace(self, **changes)


async def step_project(store):
    from src.middlewares.project_state.models import Task, TaskStatus
    from src.middlewares.project_state.workflow import ProjectWorkflow

    workflow = ProjectWorkflow(store)
    brief = await workflow.artifacts.create("p", "t", "task_brief", "实现任务，保留现有行为。")
    task = Task("t", "功能", TaskStatus.READY, "developer", frozenset(), frozenset(),
                ("功能可用",), artifact_refs=(brief["reference"],))
    await ProjectStore(store).save(ProjectSnapshot("p", "目标", (), (task,)))
    return workflow


def test_pm_static_system_and_latest_human_copy(monkeypatch):
    from langchain_core.messages import HumanMessage, SystemMessage

    from src.deep_agents.agents.pm_agent.prompts import PM_STABLE_SYSTEM_PROMPT
    from src.middlewares.acp.middleware import ACPMiddleware
    from src.middlewares.memory.pm_agent_memory import middleware as memory_module
    from src.middlewares.project_state import middleware as project_module

    async def scenario():
        store = InMemoryStore()
        workflow = await step_project(store)
        config = {"configurable": {"project_id": "p", "thread_id": "owner"}}
        project = project_module.ProjectStateMiddleware(workflow=workflow, pm_discussion_gate=True)
        memory = memory_module.PMAgentMemoryMiddleware()
        acp = ACPMiddleware(async_subagents=[{"name": "developer", "description": "开发负责人", "graph_id": "worker"}], pm_governed=True)
        first = HumanMessage(content="旧问题")
        latest = HumanMessage(content=[{"type": "text", "text": "当前问题"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}])
        baseline = SystemMessage(content=PM_STABLE_SYSTEM_PROMPT)
        captured = []

        async def model(request):
            captured.append(request)
            return request

        async def with_acp(request):
            return await acp.awrap_model_call(request, model)

        async def with_memory(request):
            return await memory.awrap_model_call(request, with_acp)

        for index in range(3):
            await ProjectStore(store).save(ProjectSnapshot("p", f"目标{index}", (), ()))
            await store.aput(("deep_loopminder", "pm_memory", "p"), str(index), {"content": "变化的记忆"})
            state = {"messages": [first, latest], "pm_memory_model_error": "模型暂不可用",
                     "async_tasks": {"w": {"run_id": f"r{index}", "status": "success"}}}
            request = StepRequest(StepRuntime(store, config, state), state, [first, latest], baseline)
            await project.awrap_model_call(request, with_memory)
        config["configurable"]["pm_discussion_mode"] = True
        await project.awrap_model_call(request, with_memory)
        assert all(item.system_message.model_dump_json() == baseline.model_dump_json() for item in captured)
        assert captured[-1].messages[0] is first
        assert all(item.messages[-1] is latest for item in captured)
        assert latest.content[0]["text"] == "当前问题"
        assert captured[-1].messages[-1].content[-1] == latest.content[-1]
        memory.context_token_limit = 100
        short_system = SystemMessage(content="固定提示词")
        long_messages = [HumanMessage(content="旧历史" * 10000), HumanMessage(content="当前问题")]
        trimmed = await memory.awrap_model_call(
            replace(request, system_message=short_system, messages=long_messages), model
        )
        assert trimmed.system_message is short_system
        assert len(trimmed.messages) == 1 and trimmed.messages[0].content == "当前问题"

    asyncio.run(scenario())


def test_pm_tools_and_project_read_live_facts():
    from src.deep_agents.agents.pm_agent.discussion import PMDiscussionMiddleware
    from src.middlewares.acp.middleware import ACPMiddleware
    from src.middlewares.approval.human_decision import HumanDecisionRequestMiddleware
    from src.middlewares.memory.pm_agent_memory.jobs import PMMemory
    from src.middlewares.memory.pm_agent_memory.middleware import (
        PMAgentMemoryMiddleware,
    )
    from src.middlewares.project_state.middleware import ProjectStateMiddleware

    async def scenario():
        store = InMemoryStore()
        workflow = await step_project(store)
        project = ProjectStateMiddleware(workflow=workflow, pm_discussion_gate=True)
        memory = PMAgentMemoryMiddleware(PMMemory(store))
        acp = ACPMiddleware(async_subagents=[{"name": "developer", "description": "开发负责人", "graph_id": "worker"}], pm_governed=True)
        tools = [tool for middleware in (project, memory, acp, PMDiscussionMiddleware(acp), HumanDecisionRequestMiddleware()) for tool in middleware.tools]
        names = {tool.name for tool in tools}
        assert len(tools) == len(names) == 21
        assert not names & {"project_context", "pm_memory_status"}
        assert {"spawn_agent", "followup_task", "send_message", "submit_pm_memory", "pm_read_bookmark"} <= names
        runtime = StepRuntime(store, {"configurable": {"project_id": "p", "thread_id": "owner"}},
                              {"async_tasks": {"w": {"run_id": "done", "status": "success"}}, "acp_delivered": {}})
        read = next(tool for tool in tools if tool.name == "project_read")
        value = await read.coroutine(runtime=runtime)
        assert value["unclaimed_runs"] == ["done"]
        assert value["review_facts"][0]["discussion_mode"] is False
        assert value["review_facts"][0]["clef_passed"] is False
        assert "validation_state" in value["review_facts"][0]
        task = await read.coroutine(runtime=runtime, task_id="t")
        assert task["task"]["task_id"] == "t" and "independent_review" in task
        runtime.config["configurable"]["pm_discussion_mode"] = True
        runtime.state["acp_delivered"] = {"done": True}
        updated = await read.coroutine(runtime=runtime)
        assert updated["discussion_mode"] is True and not updated["unclaimed_runs"]
        assert updated["review_facts"][0]["phase_label"] == "待商讨"

    asyncio.run(scenario())


def test_pm_bookmark_discovery_and_two_level_pagination():
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        ToolMessage,
        messages_to_dict,
    )

    from src.middlewares.memory.pm_agent_memory.jobs import PMMemory
    from src.middlewares.memory.pm_agent_memory.middleware import (
        PMAgentMemoryMiddleware,
    )

    async def scenario():
        store = InMemoryStore()
        source = {"thread_id": "owner", "created_at": "2026-11-27", "purpose": "记录", "project_snapshot": {"project_id": "p", "goal": "目标", "tasks": []},
                  "snapshot": messages_to_dict([HumanMessage(content="第一轮"), AIMessage(content="早期结论"),
                      HumanMessage(content="第二轮"), AIMessage(content="", tool_calls=[{"name": "project_read", "args": {}, "id": "c1"}]),
                      ToolMessage(content="重要输出" * 100, tool_call_id="c1", name="project_read"), AIMessage(content="当前结论")])}
        await store.aput(PMMemory.namespace("p"), "job", source)
        tool = next(tool for tool in PMAgentMemoryMiddleware(PMMemory(store)).tools if tool.name == "pm_read_bookmark")
        runtime = StepRuntime(store, {"configurable": {"project_id": "p", "thread_id": "owner"}}, {})
        listed = json.loads(await tool.coroutine(runtime=runtime))
        assert listed["bookmarks"][0]["job_id"] == "job"
        latest = json.loads(await tool.coroutine(runtime=runtime, job_id="job", turn_limit=1, item_limit=2))
        assert latest["boundary"] and latest["turns"][0]["turn"] == 2
        assert "输出已折叠" in latest["turns"][0]["items"][0]["output"]
        earlier_items = json.loads(await tool.coroutine(runtime=runtime, job_id="job", item_cursor=latest["turns"][0]["next_item_cursor"]))
        assert [item["type"] for item in earlier_items["items"]] == ["user_message", "tool_call"]
        earlier = json.loads(await tool.coroutine(runtime=runtime, job_id="job", cursor=latest["page"]["next_cursor"]))
        assert earlier["turns"][0]["turn"] == 1
        expanded = json.loads(await tool.coroutine(runtime=runtime, job_id="job", turn_limit=1, include_outputs=True, max_output_chars_per_item=100))
        result = next(item for item in expanded["turns"][0]["items"] if item["type"] == "tool_result")
        assert result["output"].startswith("重要输出") and len(result["output"]) == 101

    asyncio.run(scenario())


def test_pm_direct_dispatch_and_discussion_gate(monkeypatch):
    from langchain_core.messages import ToolMessage
    from langgraph.types import Command

    from src.deep_agents.agents.pm_agent.collaboration import PMWorkerGovernance
    from src.deep_agents.agents.pm_agent.discussion import DiscussionGate
    from src.middlewares.acp import server
    from src.middlewares.acp.correlation import WorkerRef
    from src.middlewares.project_state import workflow as workflow_module
    from src.middlewares.project_state.middleware import ProjectStateMiddleware
    from src.runtime.routing import WorkerCandidate

    async def scenario():
        store = InMemoryStore()
        workflow = await step_project(store)
        middleware = ProjectStateMiddleware(workflow=workflow, pm_discussion_gate=True)
        config = {"configurable": {"project_id": "p", "thread_id": "owner", "project_state_single_writer": True}}
        state = {"messages": [], "async_tasks": {}, "acp_task_names": {}}
        runtime = StepRuntime(store, config, state)
        observed = []
        candidate = WorkerCandidate("claude", "acp", frozenset({"developer"}), frozenset(), True)
        monkeypatch.setattr(server, "worker_catalog", lambda: [candidate])

        async def handler(request):
            observed.append(request)
            return Command(update={"async_tasks": {"worker": {"run_id": "r1", "agent_name": "developer", "status": "running"}}})

        request = StepRequest(runtime, state, tool_call={"name": "spawn_agent", "id": "c", "args": {"task_id": "t", "task_name": "feature", "message": "直接实现"}})
        result = await middleware._execute_tool_call(request, handler)
        assert isinstance(result, Command)
        delegated = observed[-1]
        assert delegated.runtime.config["configurable"]["_pm_phase"] == "execution"
        assert delegated.runtime.config["configurable"]["_pm_discussion_required"] is False
        binding = await store.aget(("deep_loopminder", "project_execution_refs", "p"), "r1")
        assert binding.value["phase"] == "execution"
        intent = await store.aget(("deep_loopminder", "project_dispatch_intents", "p"), "t")
        assert intent.value["status"] == "confirmed"
        ref = WorkerRef("claude", "native", "p", "t", "worker", "developer", "C:/demo")
        governance = PMWorkerGovernance(lambda: store)
        envelope = {"pm_phase": "execution", "discussion_required": False, "pm_dispatch_id": intent.value["intent_id"],
                    "owner_thread_id": "owner", "message": delegated.tool_call["args"]["message"]}
        await governance.authorize_execution(envelope, ref, {"metadata": {"run_id": "r1"}})
        import pytest
        with pytest.raises(PermissionError):
            await governance.authorize_execution({**envelope, "message": "篡改"}, ref, {"metadata": {"run_id": "r1"}})
        with pytest.raises(PermissionError):
            await governance.authorize_execution({**envelope, "owner_thread_id": "other"}, ref, {"metadata": {"run_id": "r1"}})
        namespace = ("deep_loopminder", "project_dispatch_intents", "p", "history")
        legacy = {key: value for key, value in intent.value.items() if key != "discussion_required"}
        await store.aput(namespace, intent.value["intent_id"], legacy)
        with pytest.raises(ValueError):
            await governance.authorize_execution(envelope, ref, {"metadata": {"run_id": "r1"}})
        await store.aput(namespace, intent.value["intent_id"], intent.value)
        state["async_tasks"] = {"worker": {"run_id": "r1", "agent_name": "developer", "status": "success"}}
        state["acp_task_names"] = {"worker": "feature"}

        async def steer_handler(request):
            return "injected"

        steer = replace(request, tool_call={"name": "send_message", "id": "s", "args": {"target": "feature", "message": "调整"}})
        assert await middleware._execute_tool_call(steer, steer_handler) == "injected"

        async def run_get(*args):
            return {"status": "success"}

        monkeypatch.setattr(workflow_module, "get_client", lambda **_: SimpleNamespace(runs=SimpleNamespace(get=run_get)))

        async def general_followup(request):
            observed.append(request)
            return Command(update={"async_tasks": {"worker": {"run_id": "r-general", "agent_name": "developer", "status": "pending"}}})

        follow = replace(request, tool_call={"name": "followup_task", "id": "f", "args": {"target": "feature", "message": "补充按钮交互"}})
        assert isinstance(await middleware._execute_tool_call(follow, general_followup), Command)
        assert observed[-1].runtime.config["configurable"]["_pm_discussion_required"] is False
        assert "补充按钮交互" in observed[-1].tool_call["args"]["message"]
        config["configurable"]["pm_discussion_mode"] = True
        rejected = await middleware._execute_tool_call(request, handler)
        assert isinstance(rejected, ToolMessage) and "discussion_mode_active" in rejected.content
        config["configurable"]["pm_discussion_mode"] = False
        await store.aput(("deep_loopminder", "pm_discussions", "p", "t"), "latest", {"decision_id": "d"})
        await store.aput(("deep_loopminder", "pm_discussions", "p", "t"), "d", {"decision": "pass"})
        assert "discussion_mode_active" in (await middleware._execute_tool_call(request, handler)).content
        await store.aput(("deep_loopminder", "project_execution_refs", "p"), "r1", {**binding.value, "phase": "discussion"})
        await store.adelete(("deep_loopminder", "pm_discussions", "p", "t"), "latest")
        assert "discussion_mode_active" in (await middleware._execute_tool_call(request, handler)).content
        assert "steering_requires_versioned_authorization" in (await middleware._execute_tool_call(steer, steer_handler)).content

        async def require_pass(self, *args, **kwargs):
            return {"decision_id": "d", "pm_confirmation": "已确认方案"}

        monkeypatch.setattr(DiscussionGate, "require_pass", require_pass)

        async def followup(request):
            observed.append(request)
            return Command(update={"async_tasks": {"worker": {"run_id": "r2", "agent_name": "developer", "status": "pending"}}})

        follow = replace(request, tool_call={"name": "followup_task", "id": "f", "args": {"target": "feature", "message": "请实现已讨论的按钮交互并报告验证结果"}})
        assert isinstance(await middleware._execute_tool_call(follow, followup), Command)
        assert "按钮交互" in observed[-1].tool_call["args"]["message"]
        assert observed[-1].runtime.config["configurable"]["_pm_phase"] == "execution"

    asyncio.run(scenario())


def test_conversation_discussion_mode_persists_to_history_chat_and_claims(monkeypatch):
    from src.workbench import app as workbench
    from src.workbench import context

    async def scenario():
        store = InMemoryStore()
        await step_project(store)
        calls = []
        inputs = []

        class OfficialThreads:
            async def get(self, identity):
                return {"thread_id": identity, "metadata": {"loopminder_project_id": "p", "loopminder_purpose": "pm_workbench"}}

            async def get_state(self, identity):
                return {"values": {"messages": []}}

        class Runs:
            async def stream(self, *args, **kwargs):
                calls.append(kwargs["config"])
                inputs.append(kwargs["input"])
                yield SimpleNamespace(event="updates", data={})

            async def create(self, *args, **kwargs):
                calls.append(kwargs["config"])
                return {"run_id": "claim"}

        client = SimpleNamespace(threads=OfficialThreads(), runs=Runs())
        await store.aput(THREADS, "owner", {"project_id": "p", "thread_id": "owner", "title": "会话"})

        async def provider():
            return store

        monkeypatch.setattr(context, "_injected_store", store)
        monkeypatch.setattr(threads, "get_store", provider)
        monkeypatch.setattr(workbench, "get_client", lambda **_: client)
        app = Starlette(routes=workbench.routes)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 3000)), base_url="http://127.0.0.1") as http:
            response = await http.post("/workbench/api/conversations", json={"project_id": "p", "thread_id": "owner", "discussion_mode": True})
            assert response.status_code == 200 and response.json()["discussion_mode"] is True
            assert (await http.get("/workbench/api/history?project_id=p&thread_id=owner")).json()["discussion_mode"] is True
            assert (await http.post("/workbench/api/chat", json={"project_id": "p", "thread_id": "owner", "message": "你好"})).status_code == 200
            assert calls[-1]["configurable"]["pm_discussion_mode"] is True
            assert inputs[-1]["messages"][0]["content"].startswith("[当前模式：商讨模式]")
            await http.post("/workbench/api/chat", json={"project_id": "p", "thread_id": "owner", "message": "你好", "discussion_mode": False})
            assert calls[-1]["configurable"]["pm_discussion_mode"] is False
            assert inputs[-1]["messages"][0]["content"] == "你好"
            assert (await store.aget(THREADS, "owner")).value["discussion_mode"] is False
        watcher = ClaimWatcher(store)
        assert await watcher._wake(client, "p", "owner", [("worker", {"run_id": "r", "agent_name": "developer"}, "success")], {})
        assert calls[-1]["configurable"]["pm_discussion_mode"] is False

    asyncio.run(scenario())


def test_direct_worker_graph_defers_execution_and_uses_job_lifecycle(tmp_path, monkeypatch):
    from langchain_core.messages import HumanMessage

    from src.middlewares.acp import graph as graph_module
    from src.middlewares.acp.correlation import WorkerRef
    from src.middlewares.project_state.folders import ProjectFolders
    from src.runtime.routing import WorkerCandidate

    async def scenario():
        store = InMemoryStore()
        await step_project(store)
        await ProjectFolders(store).bind("p", str(tmp_path))
        events = []

        class Worker:
            process = SimpleNamespace(returncode=None)

            async def new(self, **kwargs):
                events.append("new_readonly_session")
                return WorkerRef("claude", "native", "p", "t", "worker", "developer", str(tmp_path), isolation="native-readonly")

            async def resume(self, ref):
                events.append("resume")

            async def control(self, ref, action, payload, **kwargs):
                events.append(action)
                if action == "steer":
                    return {"outcome": "injected"}
                return SimpleNamespace(stop_reason="end_turn")

        worker = Worker()

        class Pool:
            async def get_readonly(self, *args, **kwargs):
                events.append("readonly")
                return worker

            async def get(self, *args, **kwargs):
                raise AssertionError("不能在 prepare 启动无监管可写连接")

            def execution_ticket(self, *args):
                return {"intent_id": "intent"}

            async def get_execution(self, ref, intent_id, **kwargs):
                assert events.index("begin_execution") < len(events)
                assert kwargs["job_status"] == "job-status"
                events.append("get_execution")
                return worker

            async def release_execution(self, ref):
                events.append("release")

            def active_execution(self, ref):
                return worker

        class Governance:
            async def readonly_mode(self, *args):
                return "plan"

            async def authorize_execution(self, request, ref, config):
                assert request["pm_phase"] == "execution" and request["discussion_required"] is False
                events.append("authorize")

            async def begin_execution(self, *args):
                events.append("begin_execution")
                return {"job_status": "job-status"}

            async def apply_execution_mode(self, *args):
                events.append("execution_mode")

            async def apply_discussion_mode(self, *args):
                return None

            async def finish_execution(self, *args):
                events.append("finish")

        monkeypatch.setattr(graph_module, "get_store", lambda: store)
        monkeypatch.setattr(graph_module, "project_recorder", lambda _: None)
        graph = graph_module.create_worker_graph(
            {"claude": worker}, [WorkerCandidate("claude", "acp", frozenset({"developer"}), frozenset(), True)],
            project_workers=Pool(), governance=Governance(),
        )
        payload = {"operation": "spawn", "owner_thread_id": "owner", "project_id": "p", "task_id": "t",
                   "agent_type": "developer", "message": "开发", "pm_governed": True, "pm_phase": "execution", "discussion_required": False,
                   "pm_provider_id": "claude", "pm_dispatch_id": "intent"}
        result = await graph.ainvoke({"messages": [HumanMessage(content=json.dumps(payload))]},
                                     {"configurable": {"thread_id": "worker"}, "metadata": {"run_id": "run"}})
        assert events.index("new_readonly_session") < events.index("begin_execution") < events.index("get_execution") < events.index("ask")
        assert events[-2:] == ["release", "finish"]

        async def get_state(target):
            return {"values": result}

        monkeypatch.setattr(graph_module, "get_client", lambda **_: SimpleNamespace(threads=SimpleNamespace(get_state=get_state)))
        control = graph_module.create_control_graph({"claude": worker}, project_workers=Pool())
        steered = await control.ainvoke({"target": "worker", "owner_thread_id": "owner", "message": "调整"})
        assert json.loads(steered["messages"][-1].content)["outcome"] == "injected"
        result["request"] = {**result["request"], "pm_phase": "discussion"}
        blocked = await control.ainvoke({"target": "worker", "owner_thread_id": "owner", "message": "调整"})
        assert json.loads(blocked["messages"][-1].content)["reason"] == "steering_requires_versioned_authorization"

    asyncio.run(scenario())


def test_conversations_are_independent_and_bound_to_project(monkeypatch):
    async def scenario():
        store = InMemoryStore()
        await ProjectStore(store).save(ProjectSnapshot("p1", "first", (), ()))
        await ProjectStore(store).save(ProjectSnapshot("p2", "second", (), ()))
        saved = {}

        class OfficialThreads:
            async def create(self, *, thread_id, metadata, **_):
                saved[thread_id] = {"thread_id": thread_id, "metadata": metadata}
                return saved[thread_id]

            async def get(self, thread_id):
                return saved[thread_id]

        client = SimpleNamespace(threads=OfficialThreads())
        async def get_store():
            return store
        monkeypatch.setattr(threads, "get_store", get_store)
        monkeypatch.setattr(threads, "get_client", lambda **_: client)
        app = Starlette(routes=[Route("/conversations", threads.conversations, methods=["GET", "POST"])])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 3000)),
                                     base_url="http://127.0.0.1") as http:
            first = (await http.post("/conversations", json={"project_id": "p1", "title": "A"})).json()
            second = (await http.post("/conversations", json={"project_id": "p1", "title": "B"})).json()
            third = (await http.post("/conversations", json={"project_id": "p2"})).json()
            assert len({first["thread_id"], second["thread_id"], third["thread_id"]}) == 3
            listed = (await http.get("/conversations?project_id=p1")).json()["conversations"]
            assert {row["title"] for row in listed} == {"A", "B"}
            assert await threads.project_thread(store, client, "p1", first["thread_id"]) == first["thread_id"]
            try:
                await threads.project_thread(store, client, "p2", first["thread_id"])
            except ValueError:
                pass
            else:
                raise AssertionError("cross-project thread accepted")

    asyncio.run(scenario())


def test_missing_registered_thread_restores_only_verified_checkpoint(monkeypatch):
    """A missing registry entry cannot create an empty or foreign PM session."""
    import pytest
    from langgraph.checkpoint.base import CheckpointTuple

    from src.middlewares.persistence import pm_runtime

    async def scenario():
        store = InMemoryStore()
        await store.aput(THREADS, "thread", {"project_id": "project", "thread_id": "thread"})
        checkpoint = CheckpointTuple(
            config={"configurable": {"thread_id": "thread", "checkpoint_ns": "", "checkpoint_id": "checkpoint"}},
            checkpoint={"v": 4, "id": "checkpoint", "ts": "2026-11-15T00:00:00Z",
                        "channel_values": {}, "channel_versions": {}, "versions_seen": {}, "updated_channels": None},
            metadata={"source": "loop", "step": 1, "parents": {}},
        )
        observed = []

        class OfficialThreads:
            mismatch = None
            async def get(self, _thread_id):
                request = httpx.Request("GET", "http://localhost/threads/thread")
                raise httpx.HTTPStatusError("missing", request=request,
                                            response=httpx.Response(404, request=request))
            async def create(self, **kwargs):
                observed.append(kwargs)
                returned = {**kwargs["metadata"], "graph_id": kwargs["graph_id"]}
                result = {"thread_id": kwargs["thread_id"], "metadata": returned}
                if self.mismatch == "thread_id":
                    result["thread_id"] = "other"
                elif self.mismatch:
                    returned[self.mismatch] = "other"
                return result
        official = OfficialThreads()
        client = SimpleNamespace(threads=official)

        async def load(_thread_id):
            return checkpoint
        monkeypatch.setattr(pm_runtime, "get_pm_checkpoint", load)
        assert await threads.project_thread(store, client, "project", "thread") == "thread"
        assert observed[0]["if_exists"] == "do_nothing" and observed[0]["graph_id"] == "pm_agent"
        for field in ("thread_id", "graph_id", "loopminder_project_id", "loopminder_purpose"):
            official.mismatch = field
            with pytest.raises(ValueError, match="身份不匹配"):
                await threads.project_thread(store, client, "project", "thread")
        official.mismatch = None
        before = len(observed)
        original = checkpoint
        for bad in (None, original._replace(config={"configurable": {"thread_id": "other"}})):
            checkpoint = bad
            with pytest.raises(ValueError, match="检查点"):
                await threads.project_thread(store, client, "project", "thread")
        assert len(observed) == before
        checkpoint = original
        with pytest.raises(ValueError, match="不属于"):
            await threads.project_thread(store, client, "other", "thread")
        assert len(observed) == before

    asyncio.run(scenario())


def test_claim_watcher_uses_each_conversations_owner():
    async def scenario():
        store = InMemoryStore()
        for thread_id in ("first", "second"):
            await store.aput(THREADS, thread_id, {"project_id": "p", "thread_id": thread_id})
        watcher = ClaimWatcher(store)
        calls = []
        async def values(_client, _thread):
            return {"async_tasks": {}}
        async def claimable(_client, thread_id, _values):
            return [("task", {"run_id": thread_id}, "success")]
        async def wake(_client, project_id, thread_id, _entries, _names):
            calls.append((project_id, thread_id))
            return True
        watcher._thread_values, watcher._claimable, watcher._wake = values, claimable, wake
        assert set(await watcher.poll_once(None)) == {("first", "first"), ("second", "second")}
        assert set(calls) == {("p", "first"), ("p", "second")}

    asyncio.run(scenario())


def test_claim_event_marks_run_and_blocks_worker_dispatch():
    """Claim notifications may deliver results but cannot create dispatches."""
    from types import SimpleNamespace

    from src.middlewares.project_state.middleware import ProjectStateMiddleware
    from src.workbench.context import run_config

    async def scenario():
        created = []

        async def create(*args, **kwargs):
            created.append((args, kwargs))
            return {"run_id": "claim-run"}

        watcher = ClaimWatcher()
        client = SimpleNamespace(runs=SimpleNamespace(create=create))
        entries = [("worker-thread", {"run_id": "worker-run", "agent_name": "developer"}, "success")]
        assert await watcher._wake(client, "project", "owner", entries, {})
        config = created[0][1]["config"]
        assert config["configurable"]["_pm_claim_event"] is True
        assert config["configurable"]["_pm_claim_successful_runs"] == ["worker-run"]
        assert created[0][0][0] == "owner"
        assert await watcher._already_triggered(client, "owner", "worker-run")
        async def thread_get(_thread_id):
            return {"status": "idle"}

        client.threads = SimpleNamespace(get=thread_get)
        assert await watcher._claimable(client, "owner", {
            "async_tasks": {"worker-thread": entries[0][1]},
            "acp_delivered": {"worker-run": True},
        }) == []
        assert len(created) == 1
        middleware = ProjectStateMiddleware(pm_discussion_gate=True)
        calls = []

        async def handler(request):
            calls.append(request.tool_call["name"])
            return "delivered"

        for name in ("spawn_agent", "followup_task", "begin_discussion", "begin_review"):
            request = SimpleNamespace(tool_call={"name": name, "id": "call", "args": {}},
                                      runtime=SimpleNamespace(config=config))
            result = await middleware._execute_tool_call(request, handler)
            assert "claim_event_dispatch_blocked" in result.content
        assert not calls
        request = SimpleNamespace(tool_call={"name": "wait_agent", "id": "call", "args": {}},
                                  runtime=SimpleNamespace(config=config))
        assert await middleware._execute_tool_call(request, handler) == "delivered"
        user_config = run_config("project", {})
        assert user_config["configurable"]["_pm_claim_event"] is False
        request = SimpleNamespace(tool_call={"name": "followup_task", "id": "call", "args": {}},
                                  runtime=SimpleNamespace(config=user_config))
        assert await middleware._execute_tool_call(request, handler) == "delivered"

    asyncio.run(scenario())


async def _reviewable_delivery(store, directory):
    from src.middlewares.project_state.folders import ProjectFolders
    from src.middlewares.project_state.models import Task, TaskStatus
    from src.middlewares.project_state.workflow import ProjectWorkflow

    await ProjectFolders(store).bind("p", str(directory))
    workflow = ProjectWorkflow(store)
    brief = await workflow.artifacts.create("p", "t", "task_brief", "Verify the delivery.")
    task = Task("t", "Delivery", TaskStatus.REVIEW, "developer", frozenset(), frozenset(),
                ("works",), artifact_refs=(brief["reference"],))
    await ProjectStore(store).save(ProjectSnapshot("p", "goal", (), (task,)))
    await workflow.record_start("p", "t", "owner", "developer", "development")
    await workflow.record_result("p", "owner", {
        "agent_id": "developer", "run_id": "development", "status": "success", "result": "Delivered.",
        "worker_ref": {"project_id": "p", "task_id": "t", "thread_id": "developer",
                       "role": "developer", "provider_id": "claude", "session_id": "development-session"},
    })
    return workflow


def test_human_acceptance_survives_passive_task_and_directory_changes(tmp_path, monkeypatch):
    from dataclasses import replace

    from src.middlewares.project_state.commands import TransitionTaskCommand
    from src.middlewares.project_state.models import TaskStatus
    from src.middlewares.project_state.service import apply_project_command
    from src.workbench import app as workbench
    from src.workbench import context

    async def scenario():
        store = InMemoryStore()
        workflow = await _reviewable_delivery(store, tmp_path)
        monkeypatch.setattr(context, "_injected_store", store)
        dialogs = []
        change_criteria_during_confirm = False

        async def update_criteria():
            snapshot, task = await workflow.task("p", "t")
            amended = replace(task, acceptance_criteria=(*task.acceptance_criteria, "new criterion"))
            await ProjectStore(store).save(replace(snapshot, tasks=(amended,)))

        def confirm(_title, message):
            dialogs.append(message)
            if change_criteria_during_confirm:
                asyncio.run(update_criteria())
            return True

        monkeypatch.setattr(workbench, "_confirm_local", confirm)
        app = Starlette(routes=workbench.routes)
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 3000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
            keys = {"project_id": "p", "task_id": "t"}
            original, _ = await workflow.task("p", "t")
            change_criteria_during_confirm = True
            stale_form = await http.post("/workbench/api/human-validation", json={**keys, "criteria": {"works": True}})
            assert stale_form.status_code == 409
            assert "验收标准已变化" in stale_form.json()["error"]
            change_criteria_during_confirm = False
            await ProjectStore(store).save(original)
            validation = await http.post("/workbench/api/human-validation", json={**keys, "criteria": {"works": True}})
            assert validation.status_code == 200, validation.text
            assert "未发起审查" in dialogs[-1]
            snapshot, _ = await workflow.task("p", "t")
            facts = await workflow.facts(snapshot, "t", TaskStatus.PASSED)
            assert facts.validation_accepted and not facts.human_approved
            report = await workflow.artifacts.create("p", "t", "review_note", "Additional evidence.")
            await workflow.attach("p", "t", artifacts=(report["reference"],))
            await update_criteria()
            snapshot, _ = await workflow.task("p", "t")
            passed = apply_project_command(snapshot, TransitionTaskCommand("p", "t", TaskStatus.PASSED,
                                           validation_accepted=facts.validation_accepted))
            await ProjectStore(store).save(passed)
            (tmp_path / "README.md").write_text("Minor unrelated change", encoding="utf-8")
            approval = await http.post("/workbench/api/human-approval", json={**keys, "approved": True})
            assert approval.status_code == 200, approval.text
            assert "项目目录自验收后有变化" in dialogs[-1]
            facts = await workflow.facts(passed, "t", TaskStatus.APPROVED)
            assert facts.validation_accepted and facts.human_approved
            status = (await workflow.review_status(passed))[0]
            assert status["validated_criteria"] == {"works": True}
            assert status["approval_state"] == "approved"

    asyncio.run(scenario())


def test_claim_review_is_scoped_to_received_delivery_and_skips_existing_review(tmp_path, monkeypatch):
    import json
    from dataclasses import dataclass
    from unittest.mock import AsyncMock

    from langgraph.types import Command

    from src.deep_agents.agents.pm_agent.discussion import PMDiscussionMiddleware
    from src.middlewares.acp import server
    from src.middlewares.execution.security_policy import (
        EnforcementCapability,
        EnforcementFact,
    )
    from src.runtime.routing import WorkerCandidate

    @dataclass
    class Runtime:
        store: InMemoryStore
        config: dict
        state: dict

    async def scenario():
        store = InMemoryStore()
        workflow = await _reviewable_delivery(store, tmp_path)
        worker = WorkerCandidate("claude", "acp", frozenset({"reviewer"}), frozenset(), True)
        readonly = EnforcementFact("worker", EnforcementCapability.PARTIAL, frozenset(),
                                   mechanism="fixture", gap="mode unconfirmed")
        monkeypatch.setattr(server, "worker_catalog", lambda: (worker,))
        monkeypatch.setattr(server, "inspect_worker_workspace", AsyncMock(return_value=readonly))
        spawn = AsyncMock(return_value=Command(update={"async_tasks": {"reviewer": {"run_id": "review"}}}))
        middleware = PMDiscussionMiddleware(SimpleNamespace(tools=[SimpleNamespace(name="spawn_agent", coroutine=spawn)]))
        review = next(tool.coroutine for tool in middleware.tools if tool.name == "begin_review")
        config = {"project_id": "p", "thread_id": "owner", "project_state_single_writer": True,
                  "_pm_claim_event": True, "_pm_claim_successful_runs": ["development", "other"]}
        receipts = {"acp_delivered": {"development": True, "other": True}}
        for settings, state in (
            ({**config, "_pm_claim_successful_runs": ["other"]}, receipts),
            ({**config, "thread_id": "other-owner"}, receipts),
            (config, {"acp_delivered": {"other": True}}),
        ):
            assert (await review("t", Runtime(store, {"configurable": settings}, state)))["ok"] is False
        runtime = Runtime(store, {"configurable": config}, receipts)
        assert isinstance(await review("t", runtime), Command)
        assert (await review("t", runtime))["status"] == "review_already_started"
        spawn.assert_awaited_once()
        assert spawn.await_args.kwargs["runtime"].config["configurable"]["_pm_provider_id"] == "claude"
        delivery = await workflow.delivery_subject("p", "t")
        await workflow.record_result("p", "owner", {
            "agent_id": "reviewer", "run_id": "review", "status": "success",
            "result": json.dumps({"decision": "pass", "subject": delivery["subject"],
                                  "summary": "Verified.", "findings": []}),
            "worker_ref": {"project_id": "p", "task_id": "t", "thread_id": "reviewer",
                           "role": "reviewer", "provider_id": "claude", "session_id": "review-session"},
        })
        receipt = await workflow.record_independent_review("p", "t", review_run_id="review")
        assert receipt["review_provider_id"] == "claude"
        assert (await workflow.independent_review_status("p", "t"))["status"] == "pass"

    asyncio.run(scenario())


def test_project_status_exposes_worker_recovery_without_inventing_status(monkeypatch):
    """Show the persisted task and recovery facts, including unknown values."""
    from src.deep_agents.agents.pm_agent import collaboration, discussion
    from src.middlewares.project_state.models import Task, TaskStatus
    from src.middlewares.project_state.workflow import ProjectWorkflow
    from src.workbench import app as workbench
    from src.workbench import context

    async def scenario():
        store = InMemoryStore()
        task = Task("task", "Task", TaskStatus.IN_PROGRESS, "developer", frozenset(), frozenset(), ("done",))
        await ProjectStore(store).save(ProjectSnapshot("p", "goal", (), (task,)))
        namespace = ("deep_loopminder", "project_worker_executions", "p")
        await store.aput(namespace, "execution", {
            "task_id": "task", "owner_thread_id": "owner", "official_run_id": "run",
            "recovery": {"host_instance": collaboration._HOST_INSTANCE,
                         "classification": "unknown", "job_state": "running", "worker_state": "unknown",
                         "thread_status": None, "run_status": None,
                         "dispatch_allowed": False, "reason": "无法确认进程"},
        })
        async def nothing(*_args, **_kwargs):
            return None
        async def empty(*_args, **_kwargs):
            return []
        async def review(*_args, **_kwargs):
            return {"status": "unknown"}
        monkeypatch.setattr(context, "_injected_store", store)
        monkeypatch.setattr(collaboration, "reconcile_verified_jobs", nothing)
        monkeypatch.setattr(discussion.DiscussionGate, "latest", nothing)
        monkeypatch.setattr(ProjectWorkflow, "review_status", empty)
        monkeypatch.setattr(ProjectWorkflow, "independent_review_status", review)
        app = Starlette(routes=workbench.routes)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 3000)),
                                     base_url="http://127.0.0.1") as client:
            response = await client.get("/workbench/api/project-status", params={"project_id": "p"})
            assert response.status_code == 200
            execution = response.json()["worker_executions"][0]
            assert execution["execution_id"] == "execution" and execution["task_status"] == "in_progress"
            assert execution["owner_thread_id"] == "owner" and execution["run_id"] == "run"
            assert execution["recovery"]["classification"] == "unknown"
            assert execution["recovery"]["worker_state"] == "unknown"
            assert execution["recovery"]["run_status"] is None
            assert execution["recovery"]["dispatch_allowed"] is False
            assert execution["recovery"]["reason"] == "无法确认进程"
            await store.aput(namespace, "execution", {
                "task_id": "task", "recovery": {"host_instance": "previous-host",
                "classification": "completed", "dispatch_allowed": True}})
            stale = (await client.get("/workbench/api/project-status", params={"project_id": "p"})).json()
            assert stale["worker_executions"][0]["recovery"]["classification"] == "pending"
            assert stale["worker_executions"][0]["recovery"]["dispatch_allowed"] is None
    asyncio.run(scenario())


def test_worker_recovery_classifies_and_retains_isolation(monkeypatch):
    """Only verified stop and official terminal facts permit isolation cleanup."""
    from src.deep_agents.agents.pm_agent import collaboration

    async def scenario():
        store = InMemoryStore()
        project = "recover-project"
        owner = "owner"
        await store.aput(THREADS, owner, {"project_id": project, "thread_id": owner})
        archive = ("deep_loopminder", "project_worker_executions", project)
        quarantine = ("deep_loopminder", "project_worker_quarantine", project)
        monkeypatch.setattr(collaboration, "job_observation", lambda record: {
            "state": record["test_job"], "reason": "test observation"})
        monkeypatch.setattr(collaboration, "_job_is_live", lambda record: record.get("test_live", False))

        class Threads:
            async def get(self, worker):
                if worker == "missing":
                    raise RuntimeError("registry unavailable")
                return {"thread_id": worker, "status": "idle"}
            async def get_history(self, worker, **kwargs):
                run_id = kwargs["metadata"]["run_id"]
                return [{"next": [], "values": {"owner_thread_id": "foreign" if worker == "foreign" else owner,
                         "worker_ref": {"project_id": project, "task_id": worker, "thread_id": worker},
                         "messages": [{"type": "ai", "additional_kwargs": {"acp_run_id": run_id}}]}}]
        class Runs:
            async def get(self, worker, run_id):
                return {"thread_id": worker, "run_id": run_id,
                        "status": "running" if worker in {"old", "current", "live"} else "success"}
        client = SimpleNamespace(threads=Threads(), runs=Runs())
        for worker, job, live, host in (
            ("done", "stopped_verified", False, "old-host"),
            ("foreign", "stopped_verified", False, "old-host"),
            ("old", "stopped_verified", False, "old-host"),
            ("current", "stopped_verified", False, collaboration._HOST_INSTANCE),
            ("live", "running", True, "old-host"),
            ("unknown", "running", False, "old-host"),
            ("missing", "stopped_verified", False, "old-host"),
        ):
            value = {"project_id": project, "task_id": worker, "owner_thread_id": owner,
                     "official_run_id": "run-" + worker, "execution_id": "exec-" + worker,
                     "worker_ref": {"project_id": project, "task_id": worker, "thread_id": worker},
                     "test_job": job, "test_live": live, "host_instance": host}
            await store.aput(archive, value["execution_id"], value)
            await store.aput(quarantine, worker, value)
        await collaboration.recover_worker_executions(store, client, include_completed=True)
        for worker, expected in (("done", "completed"), ("foreign", "recovery_failed"),
                                 ("old", "interrupted"), ("current", "pending"),
                                 ("live", "running"), ("unknown", "unknown"),
                                 ("missing", "recovery_failed")):
            record = (await store.aget(archive, "exec-" + worker)).value
            assert record["recovery"]["classification"] == expected
            assert (await store.aget(quarantine, worker) is None) == (worker == "done")
        await collaboration.recover_worker_executions(store, client)
        assert (await store.aget(archive, "exec-done")).value["owner_thread_id"] == owner
        assert await store.aget(quarantine, "done") is None

    asyncio.run(scenario())


def test_worker_execution_is_not_started_twice_for_same_run(tmp_path, monkeypatch):
    """A persisted run identity blocks a second execution before any dispatch claim."""
    import pytest

    from src.deep_agents.agents.pm_agent import collaboration
    from src.middlewares.acp.correlation import WorkerRef

    async def scenario():
        store = InMemoryStore()
        governance = collaboration.PMWorkerGovernance(lambda: store)
        monkeypatch.setattr(collaboration, "_job_directory", lambda: tmp_path)
        async def authorize(*_args):
            return None
        async def digest(*_args):
            return "digest"
        async def claim(*_args):
            pytest.fail("duplicate execution must not claim or start work")
        monkeypatch.setattr(governance, "authorize_execution", authorize)
        monkeypatch.setattr(governance, "claim_intent", claim)
        monkeypatch.setattr(collaboration.ProjectFolders, "content_digest", digest)
        ref = WorkerRef("claude", "session", "project", "task", "worker", "developer", str(tmp_path))
        await store.aput(("deep_loopminder", "project_worker_executions", "project"), "prior",
                         {"official_run_id": "run"})
        with pytest.raises(PermissionError, match="不能重复启动"):
            await governance.begin_execution(ref, {"owner_thread_id": "owner", "pm_dispatch_id": "intent"},
                                             {"metadata": {"run_id": "run"}}, {})
        assert await store.aget(("deep_loopminder", "project_worker_quarantine", "project"), "worker") is None

    asyncio.run(scenario())


def test_claim_watcher_retries_only_after_known_claim_finishes():
    """A failed claim can retry once; a running or unknown claim cannot."""
    async def scenario():
        statuses = {"claim-1": "running"}
        class Runs:
            async def get(self, thread_id, run_id):
                if run_id not in statuses:
                    raise RuntimeError("unavailable")
                return {"status": statuses[run_id]}
        client = SimpleNamespace(runs=Runs())
        watcher = ClaimWatcher()
        assert not await watcher._already_triggered(client, "owner", "worker-run")
        watcher._mark_triggered("worker-run", "claim-1")
        assert await watcher._already_triggered(client, "owner", "worker-run")
        statuses["claim-1"] = "error"
        assert not await watcher._already_triggered(client, "owner", "worker-run")
        watcher._mark_triggered("worker-run", "claim-2")
        statuses["claim-2"] = "error"
        assert await watcher._already_triggered(client, "owner", "worker-run")
        watcher._mark_triggered("unknown-worker", None)
        assert await watcher._already_triggered(client, "owner", "unknown-worker")

        async def failed_create(*args, **kwargs):
            raise TimeoutError("outcome unknown")
        client.runs.create = failed_create
        task = {"run_id": "uncertain", "agent_name": "worker"}
        assert not await watcher._wake(client, "project", "owner", [("worker", task, "success")], {})
        assert await watcher._already_triggered(client, "owner", "uncertain")

    asyncio.run(scenario())


def test_worker_review_owner_comes_from_matching_dispatch(monkeypatch):
    from src.workbench import worker_reviews
    async def scenario():
        store = InMemoryStore()
        async def get_store():
            return store
        monkeypatch.setattr(worker_reviews, "get_store", get_store)
        await store.aput(("deep_loopminder", "project_dispatch_intents", "p"), "task", {
            "phase": "execution", "worker_agent_id": "worker", "owner_thread": "conversation"})
        assert await _owner_for_worker({"project_id": "p", "task_id": "task", "thread_id": "worker"}) == "conversation"
        assert await _owner_for_worker({"project_id": "p", "task_id": "task", "thread_id": "other"}) is None
    asyncio.run(scenario())


def test_dispatch_reconciliation_requires_matching_owner_and_run(monkeypatch):
    """Reconciliation reads the saved claim and refuses another owner's checkpoint."""
    from src.middlewares.project_state.artifacts import content_hash
    from src.workbench import app as workbench

    async def scenario():
        store = InMemoryStore()
        await store.aput(("deep_loopminder", "project_dispatch_intents", "p", "history"),
                         "intent", {"claimed_run_id": "run"})
        async def get_store():
            return store
        class Runs:
            async def list(self, *_args, **_kwargs):
                return [{"run_id": "run", "status": "success"}]
        class Threads:
            owner = "conversation"
            async def get_history(self, *_args, **_kwargs):
                return [{"values": {"owner_thread_id": self.owner,
                        "request": {"pm_dispatch_id": "intent", "pm_phase": "execution", "message": "work"},
                        "worker_ref": {"project_id": "p", "task_id": "task", "thread_id": "worker"}}}]
        threads = Threads()
        monkeypatch.setattr(workbench, "get_store", get_store)
        monkeypatch.setattr(workbench, "get_client", lambda **_: SimpleNamespace(runs=Runs(), threads=threads))
        intent = {"worker_agent_id": "worker", "intent_id": "intent", "phase": "execution",
                  "owner_thread": "conversation", "message_digest": content_hash("work")}
        assert (await workbench._matching_dispatches("p", "task", intent))[0]["run_id"] == "run"
        threads.owner = "another-conversation"
        assert await workbench._matching_dispatches("p", "task", intent) == []

    asyncio.run(scenario())


def test_history_chat_and_decision_follow_selected_thread(monkeypatch):
    from src.workbench import app as workbench
    from src.workbench import context, decisions

    async def scenario():
        store = InMemoryStore()
        await ProjectStore(store).save(ProjectSnapshot("p", "goal", (), ()))
        records = {}
        calls = []

        class OfficialThreads:
            async def create(self, *, thread_id, metadata, **_):
                records[thread_id] = {"thread_id": thread_id, "metadata": metadata}
                return records[thread_id]

            async def get(self, thread_id):
                return records[thread_id]

            async def get_state(self, thread_id):
                return {"values": {"messages": [{"type": "ai", "content": thread_id}]}, "checkpoint": {}}

        class OfficialRuns:
            async def stream(self, thread_id, graph, **kwargs):
                calls.append((thread_id, graph, kwargs["config"]["configurable"]["project_id"]))
                yield SimpleNamespace(event="updates", data={"thread": thread_id})

        client = SimpleNamespace(threads=OfficialThreads(), runs=OfficialRuns())
        monkeypatch.setattr(context, "_injected_store", store)
        monkeypatch.setattr(workbench, "get_client", lambda **_: client)
        monkeypatch.setattr(decisions, "get_client", lambda **_: client)
        first = await threads.create_conversation(store, client, "p", "A")
        second = await threads.create_conversation(store, client, "p", "B")
        app = Starlette(routes=workbench.routes)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 3000)),
                                     base_url="http://127.0.0.1") as http:
            for row in (first, second):
                tid = row["thread_id"]
                history = await http.get("/workbench/api/history", params={"project_id": "p", "thread_id": tid})
                assert history.status_code == 200 and history.json()["messages"][0]["content"] == tid
                pending = await http.get("/workbench/api/pending-decision", params={"project_id": "p", "thread_id": tid})
                assert pending.status_code == 200 and not pending.json()["pending"]
                chat = await http.post("/workbench/api/chat", json={"project_id": "p", "thread_id": tid, "message": "hello"})
                assert chat.status_code == 200 and tid in chat.text
            missing = await http.post("/workbench/api/chat", json={"project_id": "p", "thread_id": "other", "message": "hello"})
            assert missing.status_code == 400
        assert calls == [(first["thread_id"], "pm_agent", "p"), (second["thread_id"], "pm_agent", "p")]

    asyncio.run(scenario())


def test_history_preserves_catalog_model_names_despite_redaction(monkeypatch):
    """A public catalog identifier must not become a redaction placeholder."""
    from src.middlewares.agent import user_configurable
    from src.workbench import app as workbench
    from src.workbench import context

    async def scenario():
        store = InMemoryStore()
        await ProjectStore(store).save(ProjectSnapshot("p", "goal", (), ()))
        await store.aput(THREADS, "thread", {"project_id": "p", "thread_id": "thread"})
        class Threads:
            async def get(self, thread_id):
                return {"thread_id": thread_id, "metadata": {"loopminder_project_id": "p",
                         "loopminder_purpose": "pm_workbench"}}
            async def get_state(self, _thread_id):
                return {"values": {"messages": [], "pm_model_selection": {"provider_id": "local",
                        "model_id": "public-model", "reasoning_effort": "medium"}}}
        class Catalog:
            async def list(self):
                return [{"provider_id": "local", "models": [{"id": "public-model", "reasoning_levels": ["medium"]}]}]
        monkeypatch.setattr(context, "_injected_store", store)
        monkeypatch.setattr(workbench, "get_client", lambda **_: SimpleNamespace(threads=Threads()))
        monkeypatch.setattr(user_configurable, "pm_model_catalog", lambda _store: Catalog())
        monkeypatch.setattr(workbench.TrajectoryRecorder, "clean_public",
                            lambda value: {**value, "model_id": "[redacted]"} if isinstance(value, dict) and "model_id" in value else value)
        app = Starlette(routes=workbench.routes)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 3000)),
                                     base_url="http://127.0.0.1") as client:
            response = await client.get("/workbench/api/history", params={"project_id": "p", "thread_id": "thread"})
            assert response.status_code == 200
            assert response.json()["model_selection"]["model_id"] == "public-model"

    asyncio.run(scenario())


def test_restored_run_observer_tracks_only_selected_conversation():
    """A reopened page refreshes finished work without touching a switched view."""
    import json
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the workbench observer")
    source = (Path(__file__).resolve().parents[1] / "src/workbench/app.js").read_text(encoding="utf-8")
    observer = source[source.index("async function watchRestoredRun"):source.index("async function loadProject(p, conversation)")]
    script = """
const vm = require('vm');
const assert = require('assert');
let scheduled = [], opened = 0, requests = 0;
const state = { viewVersion: 1, busy: false, busyThreads: new Set(), conversation: {thread_id: 'a'} };
const button = {disabled: false};
let result = {run: {run_id: 'r1', status: 'running'}};
const context = { state, $: () => button, api: async () => { requests++; return result; },
  setTimeout: fn => scheduled.push(fn), showError: message => { throw Error(message); },
  openConversation: async conversation => { assert.strictEqual(conversation.thread_id, 'a'); opened++; } };
vm.createContext(context);
vm.runInContext(OBSERVER, context);
(async () => {
  await context.watchRestoredRun('p', 'a', 1);
  assert(state.busy && button.disabled && scheduled.length === 1);
  result = {run: {run_id: 'r1', status: 'success'}};
  await scheduled.shift()();
  assert.strictEqual(opened, 1);
  assert(!state.busy);
  // A timer from an old conversation must not query or refresh the new one.
  state.viewVersion = 2;
  await context.watchRestoredRun('p', 'a', 1);
  assert.strictEqual(requests, 2);
  // A run started and completed by the claim watcher still refreshes history.
  state.viewVersion = 1;
  await context.watchRestoredRun('p', 'a', 1, {id: 'previous', active: false});
  assert.strictEqual(opened, 2);
  // A locally streamed run is not refreshed out from under its stream.
  state.busyThreads.add('a');
  await context.watchRestoredRun('p', 'a', 1, {id: 'previous', active: false});
  assert.strictEqual(opened, 2);
  state.busyThreads.delete('a');
  await scheduled.pop()();
  assert.strictEqual(opened, 3);
  // Switching views while the request is in flight discards its response.
  state.busy = false;
  context.api = async () => { state.viewVersion = 2; return {run: {run_id: 'r2', status: 'running'}}; };
  await context.watchRestoredRun('p', 'a', 1);
  assert(!state.busy);
  assert.strictEqual(opened, 3);
})().catch(error => { console.error(error); process.exitCode = 1; });
""".replace("OBSERVER", json.dumps(observer))
    subprocess.run([node], input=script, text=True, check=True, capture_output=True)


def test_model_switch_injects_notice_into_new_user_message_without_mutating_system_prompt(monkeypatch):
    """Switching models prepends a notice to the new user message without touching System Prompt."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from src.middlewares.agent import user_configurable
    from src.middlewares.agent.user_configurable import (
        MODEL_SWITCHED_PREFIX,
        PMModelSelectionMiddleware,
    )

    async def scenario():
        store = InMemoryStore()
        mw = PMModelSelectionMiddleware()
        runtime = SimpleNamespace(store=store, execution_info=SimpleNamespace(run_id="run-1"))
        cfg = {"configurable": {"model_provider": "local", "model_name": "m2"}}
        monkeypatch.setattr(user_configurable, "get_config", lambda: cfg)

        # First turn (no previous selection) does not emit model-switched prefix.
        first_msg = HumanMessage(id="h0", content="首句")
        first = await mw.abefore_agent({"messages": [first_msg]}, runtime)
        assert first["pm_model_changed"] is False and "messages" not in first

        # Switching from m1 to m2 prepends prefix to the new HumanMessage with same id.
        switched_msg = HumanMessage(id="h1", content="切模型后继续")
        runtime2 = SimpleNamespace(store=store, execution_info=SimpleNamespace(run_id="run-2"))
        second = await mw.abefore_agent(
            {"messages": [switched_msg], "pm_model_selection": {"provider_id": "local", "model_id": "m1", "reasoning_effort": None}},
            runtime2,
        )
        assert second["pm_model_changed"] is True
        assert second["messages"][0].id == "h1"
        assert second["messages"][0].content == MODEL_SWITCHED_PREFIX + "切模型后继续"

        # awrap_model_call keeps SystemMessage byte-for-byte identical.
        system = SystemMessage(content="静态系统提示词")
        seen = {}
        monkeypatch.setattr(user_configurable, "configured_pm_model", lambda *_a, **_kw: asyncio.sleep(0, result="model-obj"))

        async def handler(req):
            seen["system"] = req.system_message
            seen["model"] = req.model
            return "ok"

        req = SimpleNamespace(
            state={"pm_model_selection": second["pm_model_selection"], "pm_model_changed": True},
            runtime=runtime2,
            system_message=system,
            model="old",
            override=lambda **kw: SimpleNamespace(
                system_message=kw.get("system_message", system),
                model=kw.get("model", "old"),
            ),
        )
        await mw.awrap_model_call(req, handler)
        assert seen["system"] is system and seen["model"] == "model-obj"

    asyncio.run(scenario())


def test_worker_usage_includes_task_id_remaining_and_compaction_visibility(monkeypatch):
    """Worker usage endpoint exposes task_id, remaining tokens, and compaction visibility."""
    from src.middlewares.acp import server as acp_server
    from src.workbench import app as workbench
    from src.workbench import context

    async def scenario():
        store = InMemoryStore()
        await ProjectStore(store).save(ProjectSnapshot("p", "goal", (), ()))
        await store.aput(THREADS, "owner", {"project_id": "p", "thread_id": "owner"})

        class Threads:
            async def get(self, thread_id):
                return {"thread_id": thread_id, "metadata": {"loopminder_project_id": "p", "loopminder_purpose": "pm_workbench"}}

            async def get_state(self, _thread_id):
                return {"values": {"async_tasks": {"worker-1": {"agent_name": "developer", "run_id": "run-1"}}}}

            async def get_history(self, _worker_id, **_kwargs):
                return [{"values": {
                    "owner_thread_id": "owner",
                    "worker_ref": {"project_id": "p", "task_id": "t1", "thread_id": "worker-1"},
                    "messages": [{"additional_kwargs": {
                        "acp_run_id": "run-1",
                        "context_usage": {"used": 12000, "size": 200000, "remaining": 188000},
                        "compaction_visibility": "unknown",
                    }}],
                }}]

        monkeypatch.setattr(context, "_injected_store", store)
        monkeypatch.setattr(workbench, "get_client", lambda **_: SimpleNamespace(threads=Threads()))
        monkeypatch.setattr(acp_server, "project_worker_pool", lambda: None)
        app = Starlette(routes=workbench.routes)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 3000)),
                                     base_url="http://127.0.0.1") as client:
            res = await client.get("/workbench/api/worker-usage", params={"project_id": "p", "thread_id": "owner"})
            assert res.status_code == 200
            worker = res.json()["workers"][0]
            assert worker["task_id"] == "t1"
            assert worker["context_usage"]["remaining"] == 188000
            assert worker["compaction_visibility"] == "unknown"

    asyncio.run(scenario())


def test_managed_worker_settings_persists_and_validates_timeout(tmp_path, monkeypatch):
    """Managed worker settings validate timeout range and persist it without hardcoding 300."""
    import pytest

    from src.middlewares.acp import server as acp_server
    from src.middlewares.agent import pm_settings as pm_settings_module

    async def scenario():
        settings_file = tmp_path / "worker-settings.json"
        monkeypatch.setattr(pm_settings_module, "current", lambda: None)
        monkeypatch.setattr(acp_server, "_worker_settings_path", lambda: settings_file)
        saved = await acp_server.save_managed_worker_settings(
            {"enabled": False, "timeout": 1200}, provider_id="claude"
        )
        assert saved["timeout"] == 1200
        loaded = acp_server.load_managed_worker_settings("claude")
        assert loaded["timeout"] == 1200
        for bad in (10, 99999, True, "300"):
            with pytest.raises(ValueError, match="单轮超时"):
                await acp_server.save_managed_worker_settings({"enabled": False, "timeout": bad}, provider_id="claude")

    asyncio.run(scenario())
