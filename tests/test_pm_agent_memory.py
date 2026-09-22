import asyncio
import inspect
from types import SimpleNamespace

from langgraph.store.memory import InMemoryStore

import src.middlewares.memory.pm_agent_memory.middleware as memory_module
from src.middlewares.memory.pm_agent_memory import (
    PMAgentMemoryMiddleware,
    PMMemoryStore,
    memory_search,
    project_context,
    remember_project_memory,
)
from src.runtime.project import ProjectSnapshot, ProjectStore, Task, TaskStatus


def _runtime(*, project_id: str, store=None):
    return SimpleNamespace(
        config={"configurable": {"project_id": project_id}},
        store=store,
    )


def _task(task_id: str, status: TaskStatus) -> Task:
    return Task(
        task_id=task_id,
        title=f"Task {task_id}",
        status=status,
        owner_role="developer",
        required_capabilities=frozenset({"python"}),
        dependencies=frozenset(),
        acceptance_criteria=("validated",),
    )


def test_project_context_reads_authoritative_project_store() -> None:
    async def scenario() -> None:
        store = InMemoryStore()
        project_store = ProjectStore(store)
        await project_store.save(
            ProjectSnapshot(
                project_id="project-1",
                goal="Ship the PM memory slice",
                constraints=("AI branch only",),
                tasks=(
                    _task("active", TaskStatus.IN_PROGRESS),
                    _task("blocked", TaskStatus.FAILED),
                    _task("done", TaskStatus.DONE),
                ),
            )
        )

        result = await project_context.coroutine(
            runtime=_runtime(project_id="project-1", store=store)
        )

        assert "Goal: Ship the PM memory slice" in result
        assert "active" in result
        assert "blocked" in result
        assert "done" not in result
        assert "AI branch only" in result

    asyncio.run(scenario())


def test_memory_search_excludes_superseded_by_default(monkeypatch) -> None:
    async def scenario() -> None:
        store = InMemoryStore()

        async def pm_store():
            return PMMemoryStore(
                store=store,
                backend="memory",
                durability="ephemeral",
            )

        monkeypatch.setattr(memory_module, "get_pm_memory_store", pm_store)

        await remember_project_memory(
            "project-1",
            "decision:store:v1",
            "PM memory should create its own database.",
            kind="decision",
            source_refs=("issue://40",),
        )
        await remember_project_memory(
            "project-1",
            "decision:store:v2",
            "PM memory must reuse the shared durable store provider.",
            kind="decision",
            source_refs=("issue://40",),
            version="2",
            supersedes="decision:store:v1",
        )

        current = await memory_search.coroutine(
            query="shared durable store provider",
            runtime=_runtime(project_id="project-1"),
        )
        historical = await memory_search.coroutine(
            query="own database",
            runtime=_runtime(project_id="project-1"),
            include_history=True,
        )
        old = await store.aget(
            ("deep_loopminder", "pm_memory", "project-1"),
            "decision:store:v1",
        )

        assert "decision:store:v2" in current
        assert "decision:store:v1" not in current
        assert "ephemeral" in current
        assert "decision:store:v1" in historical
        assert "status=superseded" in historical
        assert old is not None
        assert old.value["superseded_by"] == "decision:store:v2"

    asyncio.run(scenario())


def test_pm_memory_does_not_depend_on_legacy_revision_policy() -> None:
    source = inspect.getsource(memory_module)

    assert "revision_policy" not in source
    assert "MemoryRevisionState" not in source


def test_pm_store_provider_reuses_existing_fallback_and_reports_ephemeral(monkeypatch) -> None:
    async def scenario() -> None:
        import src.deep_agents.db as db_module
        from src.middlewares.memory.pm_agent_memory.provider import get_pm_memory_store

        shared_store = InMemoryStore()

        async def existing_store_provider():
            return shared_store

        monkeypatch.setattr(db_module, "get_postgres_store", existing_store_provider)

        result = await get_pm_memory_store()

        assert result.store is shared_store
        assert result.backend == "memory"
        assert result.durability == "ephemeral"

    asyncio.run(scenario())


def test_middleware_exposes_only_two_pm_memory_tools() -> None:
    names = [tool.name for tool in PMAgentMemoryMiddleware.tools]

    assert names == ["project_context", "memory_search"]
