"""Official run references preserve PM source evidence without a task registry."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.messages import HumanMessage
from langgraph.store.memory import InMemoryStore

from src.middlewares.memory.pm_agent_memory import PMMemory


def test_official_queue_status_and_retry_preserve_one_frozen_source():
    async def scenario():
        store = InMemoryStore()
        runs = SimpleNamespace(
            create=AsyncMock(return_value={"run_id": "run-1"}),
            get=AsyncMock(return_value={"run_id": "run-1", "status": "error"}),
            join=AsyncMock(),
        )
        memory = PMMemory(store, client=SimpleNamespace(runs=runs))
        source = await memory.submit(
            "project",
            "pm",
            [HumanMessage(content="Original confirmed decision")],
            purpose="Keep the reason",
            job_id="decision-1",
        )
        await memory.submit(
            "project",
            "pm",
            [HumanMessage(content="Later input")],
            purpose="Repeated event",
            job_id="decision-1",
        )
        assert runs.create.await_count == 1
        assert runs.create.call_args.kwargs["multitask_strategy"] == "enqueue"
        saved = await memory.source("project", source["id"])
        assert "status" not in saved and "run_id" not in saved
        assert saved["snapshot"][0]["data"]["content"] == "Original confirmed decision"
        assert (await memory.status("project", source["id"]))["status"] == "error"
        runs.create.return_value = {"run_id": "run-2"}
        await memory.retry("project", source["id"])
        assert runs.create.call_args.kwargs["input"]["source_id"] == source["id"]
        assert runs.create.call_args.args[0] == memory.thread("project")
        assert (await memory.source("project", source["id"]))["snapshot"] == saved[
            "snapshot"
        ]
        try:
            await memory.source("another-project", source["id"])
        except KeyError:
            pass
        else:
            raise AssertionError("Source evidence escaped project scope")

    asyncio.run(scenario())
