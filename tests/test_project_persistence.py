import asyncio

from langgraph.store.memory import InMemoryStore

from src.runtime.project.models import ProjectSnapshot, Task, TaskStatus
from src.runtime.project.persistence import ProjectStore


def make_snapshot(project_id: str = "project-1") -> ProjectSnapshot:
    return ProjectSnapshot(
        project_id=project_id,
        goal="Ship safely",
        constraints=("official interfaces first",),
        tasks=(
            Task(
                task_id="task-1",
                title="Implement project persistence",
                status=TaskStatus.IN_PROGRESS,
                owner_role="developer",
                required_capabilities=frozenset({"python"}),
                dependencies=frozenset(),
                acceptance_criteria=("persist across threads",),
                priority=7,
                artifact_refs=("artifact://brief",),
                execution_refs=("run://opaque",),
            ),
        ),
    )


def test_project_snapshot_round_trips_through_official_store() -> None:
    async def scenario() -> None:
        repository = ProjectStore(InMemoryStore())
        snapshot = make_snapshot()

        await repository.save(snapshot)

        assert await repository.load(snapshot.project_id) == snapshot

    asyncio.run(scenario())


def test_same_store_is_cross_thread_project_source() -> None:
    async def scenario() -> None:
        store = InMemoryStore()
        writer = ProjectStore(store)
        reader = ProjectStore(store)
        snapshot = make_snapshot()

        await writer.save(snapshot)

        assert await reader.load("project-1") == snapshot

    asyncio.run(scenario())


def test_project_query_uses_store_namespace_not_execution_state() -> None:
    async def scenario() -> None:
        repository = ProjectStore(InMemoryStore())
        first = make_snapshot("project-1")
        second = make_snapshot("project-2")
        await repository.save(first)
        await repository.save(second)

        projects = await repository.list()

        assert {project.project_id for project in projects} == {"project-1", "project-2"}
        assert all(project.tasks[0].execution_refs == ("run://opaque",) for project in projects)

    asyncio.run(scenario())


def test_missing_project_returns_none() -> None:
    async def scenario() -> None:
        repository = ProjectStore(InMemoryStore())

        assert await repository.load("missing") is None

    asyncio.run(scenario())
