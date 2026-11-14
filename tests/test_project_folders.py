"""Project directory bindings must not leak or silently change."""

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest
from langgraph.store.memory import InMemoryStore

from src.deep_agents.agents.pm_agent.collaboration import PMWorkerGovernance
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
        from src.middlewares.acp import pool

        monkeypatch.setattr(pool, "ACPWorker", FakeWorker)
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
        graph = acp_graph.create_worker_graph({"developer": FakeWorker("unused")}, [candidate], project_workers=Pool(),
                                              governance=PMWorkerGovernance(acp_graph.get_store))
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


def test_discussion_mode_is_applied_before_the_prompt_is_sent(tmp_path, monkeypatch):
    """The adapter must report the target mode before any discussion content."""
    import json

    from langchain_core.messages import HumanMessage

    from src.middlewares.acp import graph as acp_graph
    from src.middlewares.acp.correlation import WorkerRef
    from src.middlewares.project_state.worker_modes import ProjectWorkerModes
    from src.runtime.routing.models import WorkerCandidate

    observed = []

    class FakeWorker:
        def __init__(self, cwd):
            self.cwd, self.process = cwd, SimpleNamespace(returncode=None)
            self.readonly_enforcement = "native-readonly"

        async def new(self, *, project_id, task_id, thread_id, role):
            return WorkerRef("claude", "native", project_id, task_id, thread_id, role, self.cwd,
                             isolation="native-readonly")

        async def resume(self, ref):
            assert ref.cwd == self.cwd

        async def apply_mode(self, ref, mode_id):
            observed.append(("apply", mode_id))
            return {"requested_mode": mode_id, "reported_mode": mode_id,
                    "reported_mode_source": "config_option_snapshot", "confirmed_at": "now",
                    "matched": True}

        async def control(self, ref, action, payload, *, on_update):
            # 发送内容时模式必须已经设置并核对。
            observed.append(("ask", payload.get("text")))
            return SimpleNamespace(stop_reason="end_turn")

    class Pool:
        async def get_readonly(self, provider, directory, **_):
            return FakeWorker(directory)

    async def scenario():
        from src.middlewares.project_state.artifacts import content_hash
        from src.middlewares.project_state.models import (
            ProjectSnapshot,
            Task,
            TaskStatus,
        )
        from src.middlewares.project_state.persistence import ProjectStore

        store = InMemoryStore()
        await ProjectFolders(store).bind("project", str(tmp_path))
        await ProjectWorkerModes(store).save("project", "claude", "plan")
        from src.middlewares.project_state.workflow import ProjectWorkflow

        await ProjectStore(store).save(ProjectSnapshot(
            "project", "Goal", (),
            (Task("task", "Feature", TaskStatus.READY, "developer", frozenset(),
                  frozenset(), ("Works",)),),
        ))
        workflow = ProjectWorkflow(store)
        brief = await workflow.artifacts.create("project", "task", "task_brief", "# Goal\nDiscuss")
        await ProjectStore(store).save(ProjectSnapshot(
            "project", "Goal", (),
            (Task("task", "Feature", TaskStatus.READY, "developer", frozenset(),
                  frozenset(), ("Works",), artifact_refs=(brief["reference"],)),),
        ))
        version = await workflow.input_version(*(await workflow.task("project", "task")))
        # 只读阶段要求一条已持久化的派工意图。
        intent_id = "a" * 32
        await store.aput(
            ("deep_loopminder", "project_dispatch_intents", "project", "history"), intent_id,
            {"intent_id": intent_id, "phase": "discussion", "project_id": "project",
             "task_id": "task", "owner_thread": "parent", "readonly_mechanism": "native",
             "message": "discuss this", "message_digest": content_hash("discuss this"),
             "status": "pending", "input_version": version}, index=False,
        )
        monkeypatch.setattr(acp_graph, "get_store", lambda: store)
        candidate = WorkerCandidate("claude", "acp", frozenset({"developer"}), frozenset(), True)
        graph = acp_graph.create_worker_graph({"claude": FakeWorker("unused")}, [candidate],
                                              project_workers=Pool(), governance=PMWorkerGovernance(acp_graph.get_store))
        payload = {"operation": "spawn", "owner_thread_id": "parent", "project_id": "project",
                   "task_id": "task", "agent_type": "developer", "message": "discuss this",
                   "pm_governed": True, "pm_phase": "discussion", "pm_dispatch_id": intent_id,
                   "pm_provider_id": "claude"}
        output = await graph.ainvoke(
            {"messages": [HumanMessage(content=json.dumps(payload))]},
            {"configurable": {"thread_id": "worker-thread"}, "metadata": {"run_id": "run-1"}},
        )
        kinds = [step[0] for step in observed]
        assert kinds == ["apply", "ask"], observed
        assert observed[0] == ("apply", "plan")
        assert output["mode_record"]["requested_mode"] == "plan"
        assert output["mode_record"]["reported_mode_source"] == "config_option_snapshot"

    asyncio.run(scenario())


def test_directory_digest_ignores_dependencies_and_accepts_hardlinks(tmp_path):
    """Ordinary repositories must be usable for discussion and review."""
    import os

    from src.middlewares.project_state.folders import _directory_digest

    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "node_modules" / "left-pad").mkdir(parents=True)
    (project / ".venv" / "lib").mkdir(parents=True)
    (project / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (project / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    (project / ".venv" / "lib" / "dep.py").write_text("dependency\n", encoding="utf-8")
    # 依赖目录里的硬链接不应让普通项目失败。
    os.link(project / ".venv" / "lib" / "dep.py", project / ".venv" / "lib" / "dep2.py")
    first = _directory_digest(str(project))
    assert _directory_digest(str(project)) == first

    # 依赖目录变化不影响项目内容指纹。
    (project / "node_modules" / "left-pad" / "index.js").write_text("changed\n", encoding="utf-8")
    (project / ".venv" / "lib" / "extra.py").write_text("more\n", encoding="utf-8")
    assert _directory_digest(str(project)) == first

    # 项目自身内容变化必须改变指纹。
    (project / "src" / "main.py").write_text("print('changed')\n", encoding="utf-8")
    assert _directory_digest(str(project)) != first


def test_directory_digest_records_links_without_following(tmp_path):
    """A link is fingerprinted by its target, never traversed outside the project."""
    from src.middlewares.project_state.folders import _directory_digest

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside content\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / "readme.md").write_text("project\n", encoding="utf-8")
    try:
        (project / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")
    first = _directory_digest(str(project))
    # 外部内容变化不影响项目指纹，说明没有跟随链接。
    (outside / "secret.txt").write_text("changed outside\n", encoding="utf-8")
    assert _directory_digest(str(project)) == first
