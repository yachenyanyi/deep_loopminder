"""Project directory bindings must not leak or silently change."""

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest
from langgraph.store.memory import InMemoryStore

from src.middlewares.project_state.folders import ProjectFolders


def test_folder_bindings_are_idempotent_and_isolated(tmp_path):
    async def scenario():
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        folders = ProjectFolders(InMemoryStore())
        a = await folders.bind("project-a", str(first))
        assert (await folders.bind("project-a", str(first))) == a
        assert (await folders.find_directory(str(first))) == a
        assert (await folders.bind("project-b", str(second)))["directory"] == str(second)
        with pytest.raises(ValueError, match="已绑定项目"):
            await folders.bind("project-c", str(first))
        with pytest.raises(ValueError, match="其他文件夹"):
            await folders.bind("project-a", str(second))

    asyncio.run(scenario())


def test_missing_or_relative_directory_rejected(tmp_path):
    async def scenario():
        folders = ProjectFolders(InMemoryStore())
        for path in ("relative", str(tmp_path / "missing")):
            with pytest.raises(ValueError):
                await folders.bind("project", path)

    asyncio.run(scenario())


def test_acp_worker_connections_are_scoped_to_project_directory(tmp_path, monkeypatch):
    from src.middlewares.acp import server

    class FakeWorker:
        def __init__(self, provider, command, cwd, **kwargs):
            self.provider_id, self.cwd = provider, cwd
            self.process = SimpleNamespace(returncode=None)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            self.process.returncode = 0

    async def scenario():
        first, second = tmp_path / "first", tmp_path / "second"
        first.mkdir()
        second.mkdir()
        monkeypatch.setattr(server, "ACPWorker", FakeWorker)
        async with AsyncExitStack() as stack:
            pool = server._ProjectWorkerPool(stack, [{"provider_id": "developer", "command": ["fake"], "grants": []}])
            a, b = await asyncio.gather(pool.get("developer", str(first)), pool.get("developer", str(second)))
            assert a is await pool.get("developer", str(first))
            assert a is not b
            assert a.cwd == str(first) and b.cwd == str(second)
        assert a.process.returncode == b.process.returncode == 0

    asyncio.run(scenario())


def test_acp_graph_resolves_project_folder_and_rejects_wrong_resume(tmp_path, monkeypatch):
    import json

    from langchain_core.messages import HumanMessage

    from src.middlewares.acp import graph as acp_graph
    from src.middlewares.acp.correlation import WorkerRef
    from src.runtime.routing.models import WorkerCandidate

    calls = []

    class FakeWorker:
        def __init__(self, cwd):
            self.cwd = cwd
            self.process = SimpleNamespace(returncode=None)

        async def new(self, *, project_id, task_id, thread_id, role):
            return WorkerRef("developer", "native", project_id, task_id, thread_id, role, self.cwd)

        async def resume(self, ref):
            assert ref.cwd == self.cwd

        async def control(self, ref, action, payload, *, on_update):
            calls.append((ref, action))
            return SimpleNamespace(stop_reason="end_turn")

    class Pool:
        async def get(self, provider, directory):
            assert provider == "developer"
            return FakeWorker(directory)

    async def scenario():
        store = InMemoryStore()
        await ProjectFolders(store).bind("project", str(tmp_path))
        monkeypatch.setattr(acp_graph, "get_store", lambda: store)
        candidate = WorkerCandidate("developer", "acp", frozenset({"developer"}), frozenset(), True)
        graph = acp_graph.create_worker_graph({"developer": FakeWorker("unused")}, [candidate], project_workers=Pool())
        payload = {"operation": "spawn", "owner_thread_id": "parent", "project_id": "project", "task_id": "task", "agent_type": "developer", "message": "hello"}
        output = await graph.ainvoke({"messages": [HumanMessage(content=json.dumps(payload))]}, {"configurable": {"thread_id": "worker-thread"}})
        assert output["worker_ref"]["cwd"] == str(tmp_path)
        calls.clear()
        guarded_payload = {**payload, "pm_governed": True, "pm_phase": "execution"}
        with pytest.raises(PermissionError, match="开发必须续接"):
            await graph.ainvoke({"messages": [HumanMessage(content=json.dumps(guarded_payload))]}, {"configurable": {"thread_id": "another-thread"}})
        assert not calls
        payload["operation"] = "followup"
        # A later request cannot strip governance from an existing session.
        with pytest.raises(ValueError, match="discussion_required"):
            await graph.ainvoke({
                "messages": [HumanMessage(content=json.dumps({**payload, "pm_phase": "execution"}))],
                "worker_ref": output["worker_ref"], "owner_thread_id": "parent",
                "request": {"pm_governed": True},
            }, {"configurable": {"thread_id": "worker-thread"}})
        assert not calls
        forged = {**output["worker_ref"], "cwd": str(tmp_path.parent)}
        with pytest.raises(ValueError, match="不一致"):
            await graph.ainvoke({"messages": [HumanMessage(content=json.dumps(payload))], "worker_ref": forged, "owner_thread_id": "parent"}, {"configurable": {"thread_id": "worker-thread"}})

    asyncio.run(scenario())
