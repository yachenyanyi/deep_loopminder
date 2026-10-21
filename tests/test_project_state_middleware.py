"""Official Agent tool loop exercises persistence, scope and policy boundaries."""

import asyncio

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.store.memory import InMemoryStore
from pydantic import Field

from src.middlewares.project_state import ProjectStateMiddleware, ProjectStore
from src.middlewares.project_state.tools import TransitionFacts


class ToolModel(FakeMessagesListChatModel):
    seen: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        self.seen.append(messages)
        return super()._generate(messages, *args, **kwargs)


def command(args, call_id):
    return AIMessage(content="", tool_calls=[
        {"name": "project_command", "args": args, "id": call_id, "type": "tool_call"}
    ])


def test_official_tool_loop_persists_and_refreshes_context_with_trusted_gates():
    async def scenario():
        store = InMemoryStore()
        responses = [
            command({"action": "create_project", "goal": "Ship safely"}, "create"),
            command({"action": "create_task", "task": {
                "task_id": "t1", "title": "Implement", "task_brief_ref": "brief://1",
                "acceptance_criteria": ["tests pass"],
            }}, "task"),
        ]
        for status in ("ready", "assigned", "in_progress", "dev_complete", "testing", "passed", "approved", "done"):
            responses.append(command({"action": "transition_task", "task_id": "t1", "target_status": status}, status))
        responses.append(AIMessage(content="complete"))
        facts_seen = []

        async def facts(snapshot, task_id, target):
            facts_seen.append((snapshot.tasks[0].status.value, task_id, target.value))
            return TransitionFacts(validation_accepted=True, human_approved=True)

        model = ToolModel(responses=responses)
        graph = create_agent(model, middleware=[ProjectStateMiddleware(facts_provider=facts)], store=store)
        result = await graph.ainvoke({"messages": [("user", "Plan and finish")]},
            {"configurable": {"project_id": "p1", "project_state_single_writer": True}},
        )
        assert all(m.status == "success" for m in result["messages"] if isinstance(m, ToolMessage))
        snapshot = await ProjectStore(store).load("p1")
        assert snapshot.tasks[0].status.value == "done"
        assert snapshot.tasks[0].artifact_refs == ("brief://1",)
        assert await ProjectStore(store).load("p2") is None
        assert "Project state not found: p1" in model.seen[0][0].text
        assert "Goal: Ship safely" in model.seen[1][0].text
        assert "Implement [done]" in model.seen[-1][0].text
        assert facts_seen == [("testing", "t1", "passed"), ("passed", "t1", "approved")]
    asyncio.run(scenario())


def test_readonly_and_parallel_writes_fail_without_store_mutation():
    async def scenario():
        store = InMemoryStore()
        args = {"action": "create_project", "goal": "New"}
        for writer, first in [(False, command(args, "readonly")), (True, AIMessage(content="", tool_calls=[
            {"name": "project_command", "args": args, "id": "a", "type": "tool_call"},
            {"name": "project_command", "args": args, "id": "b", "type": "tool_call"},
        ]))]:
            graph = create_agent(ToolModel(responses=[first, AIMessage(content="stop")]),
                                 middleware=[ProjectStateMiddleware()], store=store)
            result = await graph.ainvoke({"messages": [("user", "create")]},
                {"configurable": {"project_id": "p", "project_state_single_writer": writer}})
            assert all(m.status == "error" for m in result["messages"] if isinstance(m, ToolMessage))
            assert await ProjectStore(store).load("p") is None
    asyncio.run(scenario())


def test_policy_rejections_preserve_state_and_schema_excludes_authority():
    async def scenario():
        from src.middlewares.project_state import ProjectSnapshot, Task, TaskStatus
        store = InMemoryStore()
        await ProjectStore(store).save(ProjectSnapshot("p", "Test gates", (), (
            Task("t", "Test", TaskStatus.TESTING, None, frozenset(), frozenset(), ("pass",)),
        )))
        middleware = ProjectStateMiddleware()
        schema = middleware.tools[-1].tool_call_schema.model_json_schema()
        assert "runtime" not in schema["properties"]
        assert "human_approved" not in schema["properties"]
        assert "validation_accepted" not in schema["properties"]
        assert "project_id" not in schema["properties"]
        model = ToolModel(responses=[
            command({"action": "transition_task", "task_id": "t", "target_status": "passed"}, "gate"),
            command({"action": "create_task", "task": {"task_id": "bad", "title": "bad", "task_brief_ref": "b", "acceptance_criteria": ["x"], "dependencies": ["missing"]}}, "dependency"),
            AIMessage(content="stop"),
        ])
        result = await create_agent(model, middleware=[middleware], store=store).ainvoke(
            {"messages": [("user", "advance")]},
            {"configurable": {"project_id": "p", "project_state_single_writer": True}})
        errors = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(errors) == 2 and all(m.status == "error" for m in errors)
        snapshot = await ProjectStore(store).load("p")
        assert len(snapshot.tasks) == 1 and snapshot.tasks[0].status == TaskStatus.TESTING
    asyncio.run(scenario())
