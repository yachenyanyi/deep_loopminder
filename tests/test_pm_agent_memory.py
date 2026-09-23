import asyncio
import inspect
import sys
from types import SimpleNamespace

from deepagents.backends import CompositeBackend, StoreBackend
from deepagents.middleware.filesystem import FilesystemMiddleware, supports_execution
from langgraph.store.memory import InMemoryStore

import src.middlewares.memory.pm_agent_memory.middleware as memory_module
from src.middlewares.memory.pm_agent_memory import (
    PMAgentMemoryMiddleware,
    PMMemoryStore,
    memory_search,
    project_context,
    project_memory_filesystem_middleware,
    remember_project_memory,
)
from src.runtime.project import ProjectSnapshot, ProjectStore, Task, TaskStatus


def _runtime(*, project_id: str, store=None):
    return SimpleNamespace(config={"configurable": {"project_id": project_id}}, store=store)


def _task(task_id: str, status: TaskStatus) -> Task:
    return Task(task_id=task_id, title=f"Task {task_id}", status=status, owner_role="developer", required_capabilities=frozenset({"python"}), dependencies=frozenset(), acceptance_criteria=("validated",))


def test_project_context_reads_authoritative_project_store() -> None:
    async def scenario() -> None:
        store = InMemoryStore(); project_store = ProjectStore(store)
        await project_store.save(ProjectSnapshot(project_id="project-1", goal="Ship the PM memory slice", constraints=("AI branch only",), tasks=(_task("active", TaskStatus.IN_PROGRESS), _task("blocked", TaskStatus.FAILED), _task("done", TaskStatus.DONE))))
        result = await project_context.coroutine(runtime=_runtime(project_id="project-1", store=store))
        assert "Goal: Ship the PM memory slice" in result; assert "active" in result; assert "blocked" in result; assert "done" not in result; assert "AI branch only" in result
    asyncio.run(scenario())


def test_memory_search_excludes_superseded_by_default(monkeypatch) -> None:
    async def scenario() -> None:
        store = InMemoryStore()
        async def pm_store(): return PMMemoryStore(store=store, backend="memory", durability="ephemeral")
        monkeypatch.setattr(memory_module, "get_pm_memory_store", pm_store)
        await remember_project_memory("project-1", "decision:store:v1", "PM memory should create its own database.", kind="decision", source_refs=("issue://40",))
        await remember_project_memory("project-1", "decision:store:v2", "PM memory must reuse the shared durable store provider.", kind="decision", source_refs=("issue://40",), version="2", supersedes="decision:store:v1")
        current = await memory_search.coroutine(query="shared durable store provider", runtime=_runtime(project_id="project-1"))
        historical = await memory_search.coroutine(query="own database", runtime=_runtime(project_id="project-1"), include_history=True)
        old = await store.aget(("deep_loopminder", "pm_memory", "project-1"), "decision:store:v1")
        assert "decision:store:v2" in current; assert "decision:store:v1" not in current; assert "ephemeral" in current
        assert "decision:store:v1" in historical; assert "status=superseded" in historical; assert old is not None; assert old.value["superseded_by"] == "decision:store:v2"
    asyncio.run(scenario())


def test_pm_memory_does_not_depend_on_legacy_revision_policy() -> None:
    source = inspect.getsource(memory_module); assert "revision_policy" not in source; assert "MemoryRevisionState" not in source


def test_pm_store_provider_reuses_existing_fallback_and_reports_ephemeral(monkeypatch) -> None:
    async def scenario() -> None:
        from src.middlewares.memory.pm_agent_memory.provider import get_pm_memory_store
        shared_store = InMemoryStore()
        async def existing_store_provider(): return shared_store
        monkeypatch.setitem(sys.modules, "src.deep_agents.db", SimpleNamespace(get_postgres_store=existing_store_provider))
        result = await get_pm_memory_store(); assert result.store is shared_store; assert result.backend == "memory"; assert result.durability == "ephemeral"
    asyncio.run(scenario())


def test_middleware_exposes_only_two_pm_memory_tools() -> None:
    assert [tool.name for tool in PMAgentMemoryMiddleware.tools] == ["project_context", "memory_search"]


def test_official_backend_route_is_project_scoped_and_has_no_shell() -> None:
    async def scenario() -> None:
        store = InMemoryStore()
        def project_backend(project_id: str) -> CompositeBackend:
            return CompositeBackend(default=StoreBackend(namespace=lambda _runtime: ("deep_loopminder", "pm_memory_scratch", project_id), store=store), routes={"/memories/": StoreBackend(namespace=lambda _runtime: ("deep_loopminder", "pm_memory_files", project_id), store=store)})
        project_one = project_backend("project-1"); project_two = project_backend("project-2")
        write_result = await project_one.awrite("/memories/daily.md", "Project one memory"); same_project = await project_one.aread("/memories/daily.md"); other_project = await project_two.aread("/memories/daily.md")
        assert write_result.error is None; assert same_project.error is None; assert same_project.file_data is not None; assert same_project.file_data["content"] == "Project one memory"; assert other_project.error is not None
        filesystem = FilesystemMiddleware(backend=project_one); assert "execute" in {tool.name for tool in filesystem.tools}; assert supports_execution(project_one) is False
    asyncio.run(scenario())


def test_project_memory_filesystem_uses_public_fail_closed_permissions() -> None:
    filesystem, permissions = project_memory_filesystem_middleware(project_id="project-1", store=InMemoryStore())
    assert isinstance(filesystem, FilesystemMiddleware); assert supports_execution(filesystem.backend) is False
    assert [permission.mode for permission in permissions] == ["allow", "deny"]
    assert [permission.paths for permission in permissions] == [["/memories/**"], ["/**"]]
    assert "_permissions" not in inspect.signature(FilesystemMiddleware).parameters or filesystem._permissions == []


def test_project_memory_filesystem_namespace_isolated_by_project() -> None:
    async def scenario() -> None:
        store = InMemoryStore()
        project_one, _ = project_memory_filesystem_middleware(project_id="project-1", store=store); project_two, _ = project_memory_filesystem_middleware(project_id="project-2", store=store)
        write_result = await project_one.backend.awrite("/memories/decision.md", "project one only"); same_project = await project_one.backend.aread("/memories/decision.md"); other_project = await project_two.backend.aread("/memories/decision.md")
        assert write_result.error is None; assert same_project.file_data is not None; assert same_project.file_data["content"] == "project one only"; assert other_project.error is not None
    asyncio.run(scenario())
