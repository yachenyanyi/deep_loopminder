"""Behavioral tests for PM snapshots, triggers, recall and handoff barriers."""

import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import Field

from src.middlewares.memory.pm_agent_memory import (
    PMAgentMemoryMiddleware,
    PMMemoryJobs,
    memory_search,
)
from src.middlewares.memory.pm_agent_memory.files import (
    document_paths,
    memory_context,
    memory_files,
    read_document,
)
from src.runtime.project import ProjectSnapshot, ProjectStore


async def write(store, project, path, body):
    backend = memory_files(store, project)
    result = await backend.awrite(path.removeprefix("/memories"), body)
    if result.error:
        old = await read_document(store, project, path)
        result = await backend.aedit(path.removeprefix("/memories"), old, body)
    assert result.error is None


def processor_for(store, *, bodies=None):
    async def process(job):
        body = "# PM daily\nEvidence: original observation.\n" + job["bookmark"]
        if bodies is not None:
            bodies.append(job)
        await write(store, job["project_id"], job["destination"], body)

    return process


class ToolModel(FakeMessagesListChatModel):
    seen: list = Field(default_factory=list)
    tool_names: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.tool_names = [tool.name for tool in tools]
        return self

    def _generate(self, messages, *args, **kwargs):
        self.seen.append(messages)
        return super()._generate(messages, *args, **kwargs)


def test_snapshot_bookmark_metadata_and_project_isolation():
    async def scenario():
        store = InMemoryStore()
        jobs = PMMemoryJobs(store, processor=processor_for(store))
        message = HumanMessage(content="Original decision")
        job = await jobs.submit(
            "a", "thread", [message], purpose="Remember why", job_id="fixed"
        )
        message.content = "Changed later"
        await jobs.wait("a", job["id"])
        saved = await jobs.status("a", "fixed")
        assert saved["snapshot"][0]["data"]["content"] == "Original decision"
        assert saved["status"] == "completed"
        bookmark = json.loads(await read_document(store, "a", saved["bookmark"]))
        assert bookmark["context_ref"] == "pm-memory-job://fixed"
        with pytest.raises(KeyError):
            await jobs.status("b", "fixed")
        with pytest.raises(FileNotFoundError):
            await read_document(store, "b", saved["destination"])
        metadata = await store.aget(
            ("deep_loopminder", "pm_memory_documents", "a"),
            saved["destination"].removeprefix("/memories"),
        )
        assert metadata.value["version"] == 1
        assert metadata.value["source_refs"] == [saved["bookmark"]]
        again = await jobs.submit(
            "a",
            "thread",
            [HumanMessage(content="New")],
            purpose="repeat",
            job_id="fixed",
        )
        assert again["snapshot"] == saved["snapshot"]

    asyncio.run(scenario())


def test_failure_keeps_snapshot_and_retry_uses_it():
    async def scenario():
        store = InMemoryStore()
        attempts = []

        async def process(job):
            attempts.append(job["snapshot"])
            if len(attempts) == 1:
                raise RuntimeError("model unavailable")
            await processor_for(store)(job)

        jobs = PMMemoryJobs(store, processor=process)
        job = await jobs.submit(
            "a",
            "t",
            [HumanMessage(content="Evidence")],
            purpose="handoff",
            handoff=True,
        )
        with pytest.raises(RuntimeError, match="model unavailable"):
            await jobs.wait("a", job["id"])
        assert (await jobs.status("a", job["id"]))["snapshot"]
        await jobs.retry("a", job["id"])
        await jobs.wait("a", job["id"])
        assert attempts[0] == attempts[1]

    asyncio.run(scenario())


def test_readable_handoff_barrier_and_thread_selection():
    async def scenario():
        store = InMemoryStore()
        jobs = PMMemoryJobs(store, processor=processor_for(store))
        body = await jobs.prepare_handoff(
            "a", "first", [HumanMessage(content="Keep focus")]
        )
        assert "Evidence" in body
        first = await memory_context(store, "a", "first")
        other = await memory_context(store, "a", "second")
        assert "Latest handoff" in first
        assert "Latest handoff" not in other

    asyncio.run(scenario())


def test_recent_daily_is_date_window_and_map_lists_topics():
    async def scenario():
        store = InMemoryStore()
        await write(store, "a", "/memories/daily/2026-09-01.md", "old daily")
        await write(store, "a", "/memories/daily/2026-10-04.md", "recent daily")
        await write(store, "a", "/memories/daily/2026-10-06.md", "future daily")
        await write(
            store,
            "a",
            "/memories/topics/store.md",
            "# Storage decision\nUse shared provider",
        )
        context = await memory_context(
            store, "a", "t", today=date(2026, 10, 5), recent_days=3
        )
        assert "recent daily" in context
        assert "old daily" not in context and "future daily" not in context
        assert "Storage decision" in context
        assert "recent daily" not in await memory_context(
            store, "a", "t", today=date(2026, 10, 5), recent_days=0
        )
        with pytest.raises(ValueError):
            await read_document(store, "a", "/memories/../secret")

    asyncio.run(scenario())


def test_document_listing_pages_beyond_first_hundred():
    async def scenario():
        store = InMemoryStore()
        for i in range(105):
            await write(store, "a", f"/memories/topics/{i}.md", str(i))
        assert len(await document_paths(store, "a")) == 105

    asyncio.run(scenario())


def test_writer_must_change_destination_and_preserves_unrelated_metadata():
    async def scenario():
        store = InMemoryStore()
        daily = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        await write(store, "a", f"/memories/daily/{daily}.md", "Already exists")
        await write(store, "a", "/memories/topics/old.md", "Older decision")

        async def no_op(job):
            pass

        jobs = PMMemoryJobs(store, processor=no_op)
        job = await jobs.submit("a", "t", [], purpose="Remember")
        with pytest.raises(RuntimeError, match="did not update"):
            await jobs.wait("a", job["id"])
        jobs.processor = processor_for(store)
        await jobs.retry("a", job["id"])
        await jobs.wait("a", job["id"])
        assert (
            await store.aget(
                ("deep_loopminder", "pm_memory_documents", "a"), "/topics/old.md"
            )
            is None
        )

    asyncio.run(scenario())


def test_saved_running_job_can_resume_with_original_snapshot():
    async def scenario():
        store = InMemoryStore()
        jobs = PMMemoryJobs(store, processor=processor_for(store))
        job = await jobs.submit(
            "a",
            "t",
            [HumanMessage(content="Before restart")],
            purpose="Remember",
            job_id="resume",
        )
        await jobs.wait("a", "resume")
        # Simulate a process dying after work but before its completion marker.
        saved = await jobs.status("a", "resume")
        saved["status"] = "running"
        await store.aput(jobs.namespace("a"), "resume", saved)
        new_jobs = PMMemoryJobs(store, processor=processor_for(store))
        await new_jobs.resume_pending("a")
        await new_jobs.wait("a", "resume")
        assert (await new_jobs.status("a", "resume"))["snapshot"] == job["snapshot"]

    asyncio.run(scenario())


def test_pm_tools_read_full_document_and_paged_bookmark_in_same_store():
    async def scenario():
        store = InMemoryStore()
        jobs = PMMemoryJobs(store, processor=processor_for(store))
        middleware = PMAgentMemoryMiddleware(jobs)
        tools = {tool.name: tool for tool in middleware.tools}
        runtime = SimpleNamespace(
            config={"configurable": {"project_id": "a", "thread_id": "t"}},
            store=store,
            state={"messages": [HumanMessage(content="Frozen source")]},
        )
        submitted = json.loads(
            await tools["submit_pm_memory"].coroutine(
                purpose="Keep reason", runtime=runtime
            )
        )
        await jobs.wait("a", submitted["job_id"])
        job = await jobs.status("a", submitted["job_id"])
        full = json.loads(
            await tools["pm_read_memory"].coroutine(
                path=job["destination"], runtime=runtime
            )
        )
        assert "Evidence" in full["content"]
        source = json.loads(
            await tools["pm_read_bookmark"].coroutine(
                job_id=job["id"], runtime=runtime, offset=0, limit=20_000
            )
        )
        assert "Frozen source" in source["content"]
        result = await memory_search.coroutine(
            query="original observation", runtime=runtime
        )
        assert job["destination"] in result
        runtime.config["configurable"]["project_id"] = "b"
        with pytest.raises(KeyError):
            await tools["pm_read_bookmark"].coroutine(job_id=job["id"], runtime=runtime)

    asyncio.run(scenario())


def test_real_graph_preserves_prompt_and_triggers_events_once():
    async def scenario():
        store = InMemoryStore()
        await ProjectStore(store).save(
            ProjectSnapshot(
                project_id="a",
                goal="Ship middleware",
                constraints=("AI only",),
                tasks=(),
            )
        )
        calls = []
        jobs = PMMemoryJobs(store, processor=processor_for(store, bodies=calls))
        model = ToolModel(responses=[AIMessage(content="Done")])
        agent = create_agent(
            model,
            system_prompt="Stable PM prompt and Skills remain",
            middleware=[PMAgentMemoryMiddleware(jobs)],
            store=store,
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"project_id": "a", "thread_id": "t"}}
        event = {
            "id": "task-1-completed",
            "kind": "task_completed",
            "evidence_ref": "test://passed",
        }
        await agent.ainvoke(
            {"messages": [HumanMessage(content="Review")], "pm_memory_events": [event]},
            config,
        )
        await jobs.drain()
        await agent.ainvoke({"messages": [HumanMessage(content="Next")]}, config)
        await jobs.drain()
        assert len(calls) == 1
        prompt = model.seen[0][0].text
        assert "Stable PM prompt and Skills remain" in prompt
        assert "Ship middleware" in prompt and "AI only" in prompt

    asyncio.run(scenario())


def test_context_growth_trigger_does_not_repeat_without_growth():
    async def scenario():
        store = InMemoryStore()
        calls = []
        jobs = PMMemoryJobs(store, processor=processor_for(store, bodies=calls))
        agent = create_agent(
            ToolModel(responses=[AIMessage(content="ok")]),
            middleware=[PMAgentMemoryMiddleware(jobs, context_token_threshold=200)],
            store=store,
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"project_id": "a", "thread_id": "t"}}
        await agent.ainvoke(
            {"messages": [HumanMessage(content="Evidence " * 300)]}, config
        )
        await jobs.drain()
        await agent.ainvoke({"messages": [HumanMessage(content="next")]}, config)
        await jobs.drain()
        assert len(calls) == 1

    asyncio.run(scenario())


def test_file_agent_writes_via_official_tools_and_denies_bookmark_edit():
    async def scenario():
        store = InMemoryStore()
        daily = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        destination = f"/memories/daily/{daily}.md"
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "bad",
                            "name": "write_file",
                            "args": {
                                "file_path": "/memories/bookmarks/forged.json",
                                "content": "forged",
                            },
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "good",
                            "name": "write_file",
                            "args": {
                                "file_path": destination,
                                "content": "# Daily\nEvidence recorded",
                            },
                        }
                    ],
                ),
                AIMessage(content="Saved"),
            ]
        )
        jobs = PMMemoryJobs(store, model)
        job = await jobs.submit(
            "a", "t", [HumanMessage(content="Evidence")], purpose="Remember"
        )
        await jobs.wait("a", job["id"])
        assert "Evidence recorded" in await read_document(store, "a", destination)
        assert "execute" not in model.tool_names and "task" not in model.tool_names
        with pytest.raises(FileNotFoundError):
            await read_document(store, "a", "/memories/bookmarks/forged.json")

    asyncio.run(scenario())


def test_failed_partial_handoff_is_not_loaded_automatically():
    async def scenario():
        store = InMemoryStore()

        async def process(job):
            await write(store, "a", job["destination"], "Partial handoff")
            raise RuntimeError("interrupted before completion")

        jobs = PMMemoryJobs(store, processor=process)
        job = await jobs.submit("a", "t", [], purpose="handoff", handoff=True)
        with pytest.raises(RuntimeError, match="interrupted"):
            await jobs.wait("a", job["id"])
        assert "Partial handoff" not in await memory_context(store, "a", "t")

    asyncio.run(scenario())


def test_concurrent_idempotent_submissions_keep_first_snapshot():
    async def scenario():
        store = InMemoryStore()
        calls = []
        jobs = PMMemoryJobs(store, processor=processor_for(store, bodies=calls))
        first, second = await asyncio.gather(
            jobs.submit(
                "a",
                "t",
                [HumanMessage(content="First")],
                purpose="remember",
                job_id="same",
            ),
            jobs.submit(
                "a",
                "t",
                [HumanMessage(content="Second")],
                purpose="remember",
                job_id="same",
            ),
        )
        await jobs.wait("a", "same")
        assert first["snapshot"] == second["snapshot"]
        assert len(calls) == 1

    asyncio.run(scenario())


def test_bookmark_keeps_project_state_as_it_was_at_submission():
    async def scenario():
        store = InMemoryStore()
        project_store = ProjectStore(store)
        await project_store.save(
            ProjectSnapshot(
                project_id="a", goal="Original goal", constraints=(), tasks=()
            )
        )
        jobs = PMMemoryJobs(store, processor=processor_for(store))
        job = await jobs.submit("a", "t", [], purpose="remember")
        await project_store.save(
            ProjectSnapshot(
                project_id="a", goal="Changed goal", constraints=(), tasks=()
            )
        )
        await jobs.wait("a", job["id"])
        saved = await jobs.status("a", job["id"])
        assert saved["project_snapshot"]["goal"] == "Original goal"

    asyncio.run(scenario())
