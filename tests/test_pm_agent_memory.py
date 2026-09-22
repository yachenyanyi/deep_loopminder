import asyncio

from langgraph.store.memory import InMemoryStore

import src.middlewares.memory.pm_agent_memory.middleware as memory_module
import src.middlewares.memory.pm_agent_memory.provider as provider_module
from src.middlewares.memory.pm_agent_memory import PMAgentMemoryMiddleware, PMMemoryStore
from src.runtime.project import ProjectSnapshot, ProjectStore, Task, TaskStatus


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


def test_provider_reuses_existing_memory_fallback(monkeypatch) -> None:
    store = InMemoryStore()

    async def existing_store():
        return store

    monkeypatch.setattr(provider_module, "get_postgres_store", existing_store)

    provider = asyncio.run(provider_module.get_pm_memory_store())

    assert provider.store is store
    assert provider.backend == "memory"
    assert provider.durability == "ephemeral"


def test_project_context_reads_authoritative_project_store(monkeypatch) -> None:
    store = InMemoryStore()
    project_store = ProjectStore(store)
    snapshot = ProjectSnapshot(
        project_id="project-1",
        goal="Ship the PM memory slice",
        constraints=("AI branch only",),
        tasks=(
            _task("active", TaskStatus.IN_PROGRESS),
            _task("blocked", TaskStatus.FAILED),
            _task("done", TaskStatus.DONE),
        ),
    )
    asyncio.run(project_store.save(snapshot))
    monkeypatch.setattr(memory_module, "get_project_store", lambda: project_store)

    middleware = PMAgentMemoryMiddleware()
    result = asyncio.run(middleware.project_context("project-1"))

    assert "Goal: Ship the PM memory slice" in result
    assert "active" in result
    assert "blocked" in result
    assert "done" not in result
    assert "AI branch only" in result


def test_memory_search_excludes_superseded_revision(monkeypatch) -> None:
    store = InMemoryStore()

    async def memory_store():
        return PMMemoryStore(
            store=store,
            backend="memory",
            durability="ephemeral",
        )

    monkeypatch.setattr(memory_module, "get_pm_memory_store", memory_store)
    middleware = PMAgentMemoryMiddleware()

    asyncio.run(
        middleware.save_memory(
            "project-1",
            "decision:store:v1",
            "PM memory should create its own database.",
            kind="decision",
            source_ref="issue://40",
        )
    )
    asyncio.run(
        middleware.save_memory(
            "project-1",
            "decision:store:v2",
            "PM memory must reuse the shared durable store provider.",
            kind="decision",
            source_ref="issue://40",
            supersedes="decision:store:v1",
        )
    )

    result = asyncio.run(
        middleware.memory_search(
            "shared durable store provider",
            project_id="project-1",
        )
    )
    old = asyncio.run(
        store.aget(
            ("deep_loopminder", "pm_memory", "project-1"),
            "decision:store:v1",
        )
    )

    assert "decision:store:v2" in result
    assert "decision:store:v1" not in result
    assert "ephemeral" in result
    assert old is not None
    assert old.value["state"] == "superseded"
    assert old.value["superseded_by"] == "decision:store:v2"


def test_middleware_exposes_only_two_pm_memory_tools() -> None:
    names = [tool.name for tool in PMAgentMemoryMiddleware().tools]

    assert names == ["project_context", "memory_search"]
