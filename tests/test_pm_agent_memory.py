import asyncio
from types import SimpleNamespace

import pytest
from langgraph.store.memory import InMemoryStore

from src.middlewares.memory.pm_agent_memory import (
    PMMemoryMiddleware,
    memory_search,
    pm_memory_store_provider,
    project_context,
    remember_project_memory,
)
from src.middlewares.memory.pm_agent_memory.provider import PMMemoryStoreProvider
from src.runtime.project import ProjectSnapshot, Task, TaskStatus
from src.runtime.project.persistence import ProjectStore


@pytest.fixture
def memory_store(monkeypatch):
    store = InMemoryStore()

    async def get_store():
        return store

    monkeypatch.setattr(pm_memory_store_provider, "get_store", get_store)
    return store


def runtime(*, project_id: str, store=None):
    return SimpleNamespace(
        config={"configurable": {"project_id": project_id}},
        store=store,
    )


def test_project_context_reads_authoritative_project_store() -> None:
    async def scenario() -> None:
        store = InMemoryStore()
        snapshot = ProjectSnapshot(
            project_id="project-1",
            goal="Ship the PM memory layer",
            constraints=("AI branch only",),
            tasks=(
                Task(
                    task_id="memory",
                    title="Implement PM memory",
                    status=TaskStatus.IN_PROGRESS,
                    owner_role="developer",
                    required_capabilities=frozenset({"python"}),
                    dependencies=frozenset(),
                    acceptance_criteria=("PM can reorient and recall history",),
                ),
            ),
        )
        await ProjectStore(store).save(snapshot)

        result = await project_context.coroutine(
            runtime=runtime(project_id="project-1", store=store)
        )

        assert "Goal: Ship the PM memory layer" in result
        assert "memory: Implement PM memory [in_progress]" in result
        assert "AI branch only" in result

    asyncio.run(scenario())


def test_memory_search_defaults_to_active_history(memory_store) -> None:
    async def scenario() -> None:
        await remember_project_memory(
            "project-1",
            "decision-store-v1",
            "PM memory uses a separate custom database.",
            kind="decision",
            source_refs=("issue://40",),
        )
        await remember_project_memory(
            "project-1",
            "decision-store-v2",
            "PM memory reuses the existing LangGraph BaseStore provider.",
            kind="decision",
            source_refs=("issue://40",),
            version="2",
            supersedes="decision-store-v1",
        )

        current = await memory_search.coroutine(
            query="BaseStore provider",
            runtime=runtime(project_id="project-1"),
            include_history=False,
            limit=5,
        )
        historical = await memory_search.coroutine(
            query="database",
            runtime=runtime(project_id="project-1"),
            include_history=True,
            limit=5,
        )

        assert "decision-store-v2" in current
        assert "status=historical" in current
        assert "decision-store-v1" not in current
        assert "decision-store-v1" in historical
        assert "status=superseded" in historical

        old = await memory_store.aget(
            ("deep_loopminder", "pm_memory"),
            "project-1:decision-store-v1",
        )
        assert old is not None
        assert old.value["superseded_by"] == "decision-store-v2"

    asyncio.run(scenario())


def test_provider_reports_memory_fallback_as_degraded(monkeypatch) -> None:
    async def scenario() -> None:
        store = InMemoryStore()

        async def get_store():
            return store

        provider = PMMemoryStoreProvider()
        monkeypatch.setattr(
            "src.middlewares.memory.pm_agent_memory.provider.get_postgres_store",
            get_store,
        )

        status = await provider.status()

        assert status.backend == "memory"
        assert status.durability == "degraded"

    asyncio.run(scenario())


def test_pm_memory_middleware_exposes_only_two_pm_tools() -> None:
    assert [tool.name for tool in PMMemoryMiddleware.tools] == [
        "project_context",
        "memory_search",
    ]
