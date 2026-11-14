"""Mode switching must be confirmed by an adapter snapshot, never assumed."""

import asyncio
from types import SimpleNamespace

import pytest
from acp.schema import (
    LoadSessionResponse,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SetSessionConfigOptionResponse,
)

from src.deep_agents.agents.pm_agent.collaboration import PMWorkerGovernance
from src.middlewares.acp.capabilities import UnsupportedControl
from src.middlewares.acp.control import ACPWorker
from src.middlewares.acp.correlation import WorkerRef
from src.middlewares.execution.security_policy import SecurityGrant

GRANTS = frozenset({"worker.connect", "worker.set_config", "worker.set_mode", "worker.observe"})
_PENDING: dict = {}


def _response(current):
    return LoadSessionResponse(
        config_options=[
            SessionConfigOptionSelect(
                id="mode", name="Mode", type="select", current_value=current,
                options=[SessionConfigSelectOption(value="plan", name="Plan"),
                         SessionConfigSelectOption(value="read-only", name="Read-only")],
            )
        ],
        modes=None,
    )


def _worker(current="plan"):
    worker = object.__new__(ACPWorker)
    worker.provider_id = "claude"
    worker.cwd = "/tmp/project"
    worker.session_isolation = "native"
    worker.config_version = "v1"
    worker.grants = lambda _ref: SecurityGrant(GRANTS)
    worker._refs = {}
    worker._metadata = {}
    worker._usage = {}
    worker._prompt_locks = {}
    worker._turn_sinks = {}
    worker._mode_locks = {}
    worker._mode_events = []
    worker.process = SimpleNamespace(returncode=None)
    worker.timeout = 5
    worker.connection = SimpleNamespace(
        set_config_option=lambda **_kwargs: asyncio.sleep(0, result=_PENDING["response"]))
    ref = WorkerRef("claude", "s1", "p", "t", "thread", "developer", "/tmp/project",
                    isolation="native", config_version="v1")
    worker._refs["s1"] = ref
    worker._metadata["s1"] = _response(current)
    _PENDING["response"] = SetSessionConfigOptionResponse(
        config_options=_response(current).config_options)
    return worker, ref


def _adapter_returns(worker, current):
    """Make the stubbed adapter answer with a snapshot in the given mode."""
    _PENDING["response"] = SetSessionConfigOptionResponse(
        config_options=_response(current).config_options)
    return worker


def test_apply_mode_uses_config_snapshot_as_confirmation():
    """A matched snapshot is the only thing reported as confirmed."""

    async def scenario():
        worker, ref = _worker("plan")
        _adapter_returns(worker, "read-only")
        result = await worker.apply_mode(ref, "read-only")
        assert result["requested_mode"] == "read-only"
        assert result["reported_mode"] == "read-only"
        assert result["reported_mode_source"] == "config_option_snapshot"
        assert result["matched"] is True
        # The live metadata now reflects the snapshot, not the request.
        assert worker.mode_state("s1")["current"] == "read-only"

    asyncio.run(scenario())


def test_apply_mode_reports_adapter_fallback_without_claiming_success():
    """An adapter that returns a different mode is not treated as switched."""

    async def scenario():
        worker, ref = _worker("plan")
        _adapter_returns(worker, "plan")
        result = await worker.apply_mode(ref, "read-only")
        assert result["requested_mode"] == "read-only"
        assert result["reported_mode"] == "plan"
        assert result["matched"] is False

    asyncio.run(scenario())


def test_apply_mode_rejects_modes_the_adapter_never_offered():
    """Unadvertised modes fail instead of being attempted or assumed."""

    async def scenario():
        worker, ref = _worker("plan")
        worker.connection.set_config_option = lambda **_kwargs: pytest.fail("must not call the adapter")
        with pytest.raises(UnsupportedControl):
            await worker.apply_mode(ref, "agent-full-access")

    asyncio.run(scenario())


def test_mode_events_never_overwrite_a_confirmed_snapshot():
    """Adapter events are recorded separately and cannot confirm a request."""

    async def scenario():
        worker, ref = _worker("plan")
        # An unrelated adapter event arrives on a session that also tracks
        # legacy mode state, mirroring adapters that still send both.
        worker._metadata["s1"] = SimpleNamespace(
            config_options=_response("plan").config_options,
            modes=SimpleNamespace(available_modes=[], current_mode_id="plan"),
            model_copy=lambda update: SimpleNamespace(
                config_options=update["config_options"],
                modes=worker._metadata["s1"].modes),
        )
        ref_snapshot = _response("read-only")
        _PENDING["response"] = SetSessionConfigOptionResponse(config_options=ref_snapshot.config_options)
        worker._mode_locks.clear()
        applied = await worker.apply_mode(ref, "read-only")
        assert applied["reported_mode"] == "read-only"
        worker._update_metadata("s1", SimpleNamespace(
            session_update="current_mode_update", current_mode_id="plan"))
        assert worker.mode_events("s1")[-1]["mode"] == "plan"
        # The snapshot confirmed the switch; the event is recorded separately.
        assert worker.mode_state("s1")["current"] == "read-only"

    asyncio.run(scenario())


def test_mode_state_reports_unavailable_when_adapter_offers_no_option():
    """Missing adapter data is reported as unavailable, never guessed."""

    async def scenario():
        worker, _ = _worker("plan")
        worker._metadata["s1"] = SimpleNamespace(config_options=[], modes=None)
        state = worker.mode_state("s1")
        assert state["available"] is False
        assert state["modes"] == []
        assert state["reason"] == "adapter_did_not_advertise_mode_option"

    asyncio.run(scenario())


def test_saved_default_is_per_project_and_provider():
    """A saved target is a preference; it never reports an observed mode."""
    from langgraph.store.memory import InMemoryStore

    from src.middlewares.project_state.worker_modes import ProjectWorkerModes

    async def scenario():
        modes = ProjectWorkerModes(InMemoryStore())
        await modes.save("project-a", "claude", "plan")
        await modes.save("project-a", "codex", "read-only")
        await modes.save("project-b", "claude", "default")
        assert (await modes.get("project-a", "claude"))["mode_id"] == "plan"
        assert (await modes.get("project-a", "codex"))["mode_id"] == "read-only"
        assert (await modes.get("project-b", "claude"))["mode_id"] == "default"
        saved = await modes.get("project-a", "claude")
        # 只保存目标值，不写入任何“实际报告”字段。
        assert set(saved) == {"project_id", "provider_id", "mode_id"}

    asyncio.run(scenario())


def test_saved_default_rejects_invalid_provider_and_mode():
    from langgraph.store.memory import InMemoryStore

    from src.middlewares.project_state.worker_modes import ProjectWorkerModes

    async def scenario():
        modes = ProjectWorkerModes(InMemoryStore())
        with pytest.raises(ValueError):
            await modes.save("project", "other", "plan")
        with pytest.raises(ValueError):
            await modes.save("project", "claude", "   ")
        with pytest.raises(ValueError):
            await modes.get("project", "other")

    asyncio.run(scenario())


def test_writable_modes_are_flagged_not_refused():
    """A user may choose a writable mode; the host must label it, not block it."""
    from src.middlewares.acp.pool import READONLY_MODES, mode_can_write

    assert "plan" in READONLY_MODES["claude"]
    assert "read-only" in READONLY_MODES["codex"]
    # 未知模式按可写处理，不能被当成安全模式展示。
    assert mode_can_write("claude", "acceptEdits") is True
    assert mode_can_write("claude", "bypassPermissions") is True
    assert mode_can_write("codex", "workspace-write") is True
    assert mode_can_write("codex", "danger-full-access") is True
    assert mode_can_write("claude", "invented") is True
    assert mode_can_write("claude", "plan") is False
    assert mode_can_write("codex", "read-only") is False


def test_writable_mode_is_reported_as_partial_boundary():
    """A writable mode must never be recorded as an enforceable read-only fact."""
    from src.middlewares.acp.pool import ProjectWorkerPool
    from src.middlewares.execution.security_policy import EnforcementCapability

    read_only = ProjectWorkerPool._readonly_fact("claude", "plan")
    assert read_only.capability is EnforcementCapability.ENFORCEABLE
    assert read_only.gap is None

    writable = ProjectWorkerPool._readonly_fact("claude", "acceptEdits")
    assert writable.capability is EnforcementCapability.PARTIAL
    assert writable.gap and "can modify the project" in writable.gap
    assert "acceptEdits" in writable.mechanism


def test_graph_persists_reported_mode_for_the_run(tmp_path, monkeypatch):
    """The worker graph must store what the adapter reported, keyed by the Run."""
    import json

    from langchain_core.messages import HumanMessage
    from langgraph.store.memory import InMemoryStore

    from src.middlewares.acp import graph as acp_graph
    from src.middlewares.acp.correlation import WorkerRef
    from src.middlewares.project_state.artifacts import content_hash
    from src.middlewares.project_state.folders import ProjectFolders
    from src.middlewares.project_state.models import ProjectSnapshot, Task, TaskStatus
    from src.middlewares.project_state.persistence import ProjectStore
    from src.middlewares.project_state.worker_modes import ProjectWorkerModes
    from src.middlewares.project_state.workflow import ProjectWorkflow
    from src.runtime.routing.models import WorkerCandidate

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
            return {"requested_mode": mode_id, "reported_mode": mode_id,
                    "reported_mode_source": "config_option_snapshot", "confirmed_at": "now",
                    "matched": True}

        async def control(self, ref, action, payload, *, on_update):
            return SimpleNamespace(stop_reason="end_turn")

    class Pool:
        async def get_readonly(self, provider, directory, **_):
            return FakeWorker(directory)

    async def scenario():
        store = InMemoryStore()
        await ProjectFolders(store).bind("project", str(tmp_path))
        await ProjectWorkerModes(store).save("project", "claude", "plan")
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
        message = "discuss this"
        intent_id = "b" * 32
        await store.aput(
            ("deep_loopminder", "project_dispatch_intents", "project", "history"), intent_id,
            {"intent_id": intent_id, "phase": "discussion", "project_id": "project",
             "task_id": "task", "owner_thread": "parent", "readonly_mechanism": "native",
             "readonly_capability": "enforceable", "message": message,
             "message_digest": content_hash(message), "status": "pending",
             "input_version": version}, index=False,
        )
        monkeypatch.setattr(acp_graph, "get_store", lambda: store)
        candidate = WorkerCandidate("claude", "acp", frozenset({"developer"}), frozenset(), True)
        graph = acp_graph.create_worker_graph({"claude": FakeWorker("unused")}, [candidate],
                                              project_workers=Pool(), governance=PMWorkerGovernance(acp_graph.get_store))
        payload = {"operation": "spawn", "owner_thread_id": "parent", "project_id": "project",
                   "task_id": "task", "agent_type": "developer", "message": message,
                   "pm_governed": True, "pm_phase": "discussion",
                   "pm_dispatch_id": intent_id, "pm_provider_id": "claude"}
        await graph.ainvoke(
            {"messages": [HumanMessage(content=json.dumps(payload))]},
            {"configurable": {"thread_id": "worker-thread"}, "metadata": {"run_id": "run-1"}},
        )
        # 关键：运行记录必须能读到适配器回报，而不是只有图内部状态。
        item = await store.aget(("deep_loopminder", "project_run_modes", "project"), "run-1")
        assert item is not None, "图未把模式回报写入运行记录"
        assert item.value["reported_mode"] == "plan"
        assert item.value["reported_mode_source"] == "config_option_snapshot"

    asyncio.run(scenario())


def test_discussion_defaults_to_plan_only_when_advertised(tmp_path, monkeypatch):
    """需求默认商讨模式 plan：适配器提供就请求并记录来源；不提供则明确失败。

    默认 plan 与用户显式保存的模式都不可用时一律失败并提示选择可用模式，
    不发送商讨内容，不静默降级。
    """
    import json

    from langchain_core.messages import HumanMessage
    from langgraph.store.memory import InMemoryStore

    from src.middlewares.acp import graph as acp_graph
    from src.middlewares.acp.control import UnsupportedControl
    from src.middlewares.acp.correlation import WorkerRef
    from src.middlewares.project_state.artifacts import content_hash
    from src.middlewares.project_state.folders import ProjectFolders
    from src.middlewares.project_state.models import ProjectSnapshot, Task, TaskStatus
    from src.middlewares.project_state.persistence import ProjectStore
    from src.middlewares.project_state.worker_modes import ProjectWorkerModes
    from src.middlewares.project_state.workflow import ProjectWorkflow
    from src.runtime.routing.models import WorkerCandidate

    def make_worker(advertise_plan):
        class FakeWorker:
            requests = []  # 按类共享：Pool 每次新建实例，测试断言读取类属性。
            controls = []

            def __init__(self, cwd):
                self.cwd, self.process = cwd, SimpleNamespace(returncode=None)
                self.readonly_enforcement = "native-readonly"

            async def new(self, *, project_id, task_id, thread_id, role):
                return WorkerRef("claude", "native", project_id, task_id, thread_id, role,
                                 self.cwd, isolation="native-readonly")

            async def resume(self, ref):
                pass

            async def apply_mode(self, ref, mode_id):
                self.requests.append(mode_id)
                if not (advertise_plan and mode_id == "plan"):
                    raise UnsupportedControl("requested mode was not advertised")
                return {"requested_mode": mode_id, "reported_mode": mode_id,
                        "reported_mode_source": "config_option_snapshot",
                        "confirmed_at": "now", "matched": True}

            async def control(self, ref, action, payload, *, on_update):
                self.controls.append(action)
                return SimpleNamespace(stop_reason="end_turn")

        return FakeWorker

    class Pool:
        def __init__(self, worker):
            self.worker = worker

        async def get_readonly(self, provider, directory, **_):
            return self.worker(directory)

    async def run_graph(store, worker, tmp_path):
        await ProjectFolders(store).bind("project", str(tmp_path))
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
        message = "discuss this"
        intent_id = "c" * 32
        await store.aput(
            ("deep_loopminder", "project_dispatch_intents", "project", "history"), intent_id,
            {"intent_id": intent_id, "phase": "discussion", "project_id": "project",
             "task_id": "task", "owner_thread": "parent", "readonly_mechanism": "native",
             "readonly_capability": "enforceable", "message": message,
             "message_digest": content_hash(message), "status": "pending",
             "input_version": version}, index=False,
        )
        monkeypatch.setattr(acp_graph, "get_store", lambda: store)
        candidate = WorkerCandidate("claude", "acp", frozenset({"developer"}), frozenset(), True)
        graph = acp_graph.create_worker_graph({"claude": worker("unused")}, [candidate],
                                              project_workers=Pool(worker), governance=PMWorkerGovernance(acp_graph.get_store))
        payload = {"operation": "spawn", "owner_thread_id": "parent", "project_id": "project",
                   "task_id": "task", "agent_type": "developer", "message": message,
                   "pm_governed": True, "pm_phase": "discussion",
                   "pm_dispatch_id": intent_id, "pm_provider_id": "claude"}
        await graph.ainvoke(
            {"messages": [HumanMessage(content=json.dumps(payload))]},
            {"configurable": {"thread_id": "worker-thread"}, "metadata": {"run_id": "run-d1"}},
        )
        return await store.aget(("deep_loopminder", "project_run_modes", "project"), "run-d1")

    async def scenario():
        # 适配器提供 plan：未保存任何模式时默认请求 plan，记录默认来源。
        worker = make_worker(advertise_plan=True)
        store = InMemoryStore()
        item = await run_graph(store, worker, tmp_path)
        assert item is not None and worker.requests == ["plan"]
        assert item.value["requested_mode"] == "plan"
        assert item.value["requested_mode_source"] == "default_plan"
        assert worker.controls == ["ask"]

        # 适配器未提供默认 plan：明确失败并提示选择模式，不静默继续发送。
        worker = make_worker(advertise_plan=False)
        store = InMemoryStore()
        raised = None
        try:
            await run_graph(store, worker, tmp_path)
        except PermissionError as exc:
            raised = str(exc)
        assert raised and "plan" in raised and "选择" in raised
        assert worker.requests == ["plan"]
        assert worker.controls == []  # 未发送任何商讨内容。
        assert await store.aget(("deep_loopminder", "project_run_modes", "project"), "run-d1") is None

        # 用户显式保存的模式不可用：仍然失败，不静默降级。
        worker = make_worker(advertise_plan=False)
        store = InMemoryStore()
        await ProjectWorkerModes(store).save("project", "claude", "read-only")
        raised = False
        try:
            await run_graph(store, worker, tmp_path)
        except UnsupportedControl:
            raised = True
        assert raised
        assert worker.controls == []

    asyncio.run(scenario())


def test_readonly_fact_does_not_claim_readonly_capability_when_unconfirmed():
    """An unconfirmed mode must not advertise an enforced read-only capability."""
    from src.middlewares.acp.pool import ProjectWorkerPool
    from src.middlewares.execution.security_policy import EnforcementCapability

    unconfirmed = ProjectWorkerPool._readonly_fact("claude", "unconfirmed")
    assert unconfirmed.capability is EnforcementCapability.PARTIAL
    assert unconfirmed.gap and "not yet confirmed" in unconfirmed.gap
    assert unconfirmed.enforced_capabilities == frozenset()


def test_acp_mutating_kinds_require_approval():
    """Anything that can change the project or session must not be auto-allowed."""
    from src.middlewares.approval.decision import RiskLevel
    from src.middlewares.approval.risk_analyzer import ApprovalConfig, RiskAnalyzer

    analyzer = RiskAnalyzer(ApprovalConfig())
    for kind in ("edit", "execute", "delete", "move", "fetch", "switch_mode",
                 "other", "some_future_kind"):
        assert analyzer.analyze(f"acp.{kind}", {}) is RiskLevel.MEDIUM, kind
    for kind in ("read", "search", "think"):
        assert analyzer.analyze(f"acp.{kind}", {}) is RiskLevel.LOW, kind


def test_acp_mutation_is_not_auto_allowed_by_the_policy():
    """The policy provider must not return 'allowed' for a mutating ACP request."""
    import asyncio

    from src.middlewares.approval.builtin import YamlPolicyProvider
    from src.middlewares.approval.request import ApprovalRequest

    provider = YamlPolicyProvider()

    def request(kind):
        return ApprovalRequest(tool_name=f"acp.{kind}", tool_input={}, agent_id="claude",
                               thread_id="thread", context={})

    async def scenario():
        for kind in ("edit", "delete", "move", "execute", "switch_mode"):
            decision = await provider.aevaluate(request(kind))
            assert not decision.allow, f"{kind} must not be auto-allowed"

    asyncio.run(scenario())


def test_readonly_connection_is_not_reused_across_modes(tmp_path, monkeypatch):
    """Real get_readonly calls: a mode change must not inherit the old fact."""
    from contextlib import AsyncExitStack

    from src.middlewares.acp import pool as pool_module

    created = []

    class FakeWorker:
        def __init__(self, provider, command, cwd, **kwargs):
            self.provider_id, self.cwd = provider, cwd
            self.process = SimpleNamespace(returncode=None)
            self.readonly_enforcement = kwargs["readonly_enforcement"]
            self._prompt_locks = {}
            created.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            self.process.returncode = 0

    monkeypatch.setattr(pool_module, "ACPWorker", FakeWorker)

    async def scenario():
        async with AsyncExitStack() as stack:
            pool = pool_module.ProjectWorkerPool(stack, [{
                "provider_id": "claude", "command": ["fake"], "grants": [],
                "runtime_dir": str(tmp_path), "node": "node",
            }])
            first = await pool.get_readonly("claude", str(tmp_path), mode_id="plan")
            again = await pool.get_readonly("claude", str(tmp_path), mode_id="plan")
            assert first is again, "同一模式应复用连接"
            switched = await pool.get_readonly("claude", str(tmp_path), mode_id="acceptEdits")
            assert switched is not first, "切换模式必须拿到新连接，否则边界事实是旧的"
            assert first.readonly_enforcement.capability.value == "enforceable"
            assert switched.readonly_enforcement.capability.value == "partial"
            assert switched.readonly_enforcement.gap

    asyncio.run(scenario())


def test_execution_is_registered_and_released_after_readonly_connections(tmp_path, monkeypatch):
    """Switching to development must register the key release_execution looks up."""
    from contextlib import AsyncExitStack

    from src.middlewares.acp import pool as pool_module
    from src.middlewares.acp.correlation import WorkerRef

    created = []

    class FakeWorker:
        def __init__(self, provider, command, cwd, **kwargs):
            self.provider_id, self.cwd = provider, cwd
            self.process = SimpleNamespace(returncode=None)
            self.readonly_enforcement = kwargs.get("readonly_enforcement")
            self.review = kwargs.get("review")
            self._prompt_locks = {}
            created.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            self.process.returncode = 0

    monkeypatch.setattr(pool_module, "ACPWorker", FakeWorker)

    async def scenario():
        async with AsyncExitStack() as stack:
            async def review(_request, _decision):
                return False

            pool = pool_module.ProjectWorkerPool(stack, [{
                "provider_id": "claude", "command": ["fake"], "grants": [],
                "runtime_dir": str(tmp_path), "node": "node",
            }], execution_review=review)
            first = await pool.get_readonly("claude", str(tmp_path), mode_id="plan", session_key="thread")
            second = await pool.get_readonly("claude", str(tmp_path), mode_id="acceptEdits", session_key="thread")
            config_version = pool._versions["claude"]
            ref = WorkerRef("claude", "session", "project", "task", "thread", "developer",
                            str(tmp_path), isolation="native-readonly",
                            config_version=config_version)
            execution = await pool.get_execution(ref, "a" * 32)
            assert execution not in (first, second)
            assert execution.review is review
            assert first.review is None and second.review is None
            # 目标会话的只读连接必须关闭，不能留下持有同一会话的进程。
            assert first.process.returncode == 0
            assert second.process.returncode == 0
            # 重复取用返回同一连接。
            assert await pool.get_execution(ref, "a" * 32) is execution
            # 释放必须成功，且之后确实不再登记该连接。
            await pool.release_execution(ref)
            assert execution.process.returncode == 0
            assert not pool._execution_connections

    asyncio.run(scenario())


def test_tool_config_cannot_downgrade_acp_mutations():
    """Disabling approval for one tool must not turn an ACP mutation into auto-allow."""
    from src.middlewares.approval.decision import RiskLevel
    from src.middlewares.approval.risk_analyzer import ApprovalConfig, RiskAnalyzer

    config = ApprovalConfig(tool_config={"acp.edit": {"enabled": False}})
    analyzer = RiskAnalyzer(config)
    assert analyzer.analyze("acp.edit", {}) is RiskLevel.MEDIUM


def test_live_mode_state_filters_by_provider_and_directory(monkeypatch):
    """The workbench must not show another provider's or project's modes."""
    from types import SimpleNamespace

    from src.middlewares.acp import server
    from src.middlewares.acp.pool import ProjectWorkerPool

    def worker(provider, cwd, modes):
        w = SimpleNamespace(provider_id=provider, cwd=cwd, _metadata={"s": None})
        w.mode_state = lambda session_id, modes=modes: {
            "available": True, "reason": None, "modes": modes, "current": modes[0]["id"]}
        return w

    claude = worker("claude", r"C:\projects\a", [{"id": "plan", "name": "Plan", "description": None}])
    codex = worker("codex", r"C:\projects\a", [{"id": "read-only", "name": "Read-only", "description": None}])
    other_project = worker("claude", r"C:\projects\b", [{"id": "plan", "name": "Plan", "description": None}])
    pool = ProjectWorkerPool(None, [])
    pool._readonly_connections = {"x": codex, "y": other_project}
    pool._connections = {"z": claude}
    monkeypatch.setattr(server, "_workers", {})
    monkeypatch.setattr(server, "_project_workers", pool)

    async def scenario():
        # 找到本项目的 claude 连接。
        state = server.discover_live_modes("claude", r"C:\projects\a")
        assert state["modes"][0]["id"] == "plan"
        # 查询 claude 时，不能拿 codex 的会话充当结果。
        only_codex = ProjectWorkerPool(None, [])
        only_codex._readonly_connections = {"x": codex}
        monkeypatch.setattr(server, "_project_workers", only_codex)
        state = server.discover_live_modes("claude", r"C:\projects\a")
        assert state["available"] is False
        # 只找到其他提供方的连接，等同于本项目没有可用连接。
        assert state["reason"] == "worker_not_connected"
        # 其他项目的会话不得出现在本项目的结果里。
        other_pool = ProjectWorkerPool(None, [])
        other_pool._readonly_connections = {"y": other_project}
        monkeypatch.setattr(server, "_project_workers", other_pool)
        state = server.discover_live_modes("claude", r"C:\projects\a")
        assert state["available"] is False

    asyncio.run(scenario())


def test_workspace_inspection_returns_fact_and_checks_directory(tmp_path):
    """PM sees the capability fact, while the pool keeps the worker object."""
    from src.middlewares.acp.pool import ProjectWorkerPool

    async def scenario():
        pool = ProjectWorkerPool(None, [])
        fact = object()
        worker = SimpleNamespace(cwd=str(tmp_path), readonly_enforcement=fact)
        requests = []

        async def get_readonly(provider, directory, *, mode_id):
            requests.append((provider, directory, mode_id))
            return worker

        pool.get_readonly = get_readonly
        assert await pool.inspect_workspace("claude", str(tmp_path), mode_id="plan") is fact
        assert requests == [("claude", str(tmp_path), "plan")]
        worker.cwd = str(tmp_path / "different")
        with pytest.raises(ValueError, match="目录身份"):
            await pool.inspect_workspace("claude", str(tmp_path))
        worker.cwd = str(tmp_path)
        worker.readonly_enforcement = None
        with pytest.raises(ValueError, match="执行边界"):
            await pool.inspect_workspace("claude", str(tmp_path))

    asyncio.run(scenario())


def test_readonly_fact_names_the_saved_mode_not_unconfirmed():
    """The pre-check connection must be opened under the saved mode.

    begin_discussion passes the project's saved mode into get_readonly; the
    resulting fact must therefore name that mode instead of reporting that the
    permission mode is still unconfirmed.
    """
    from src.middlewares.acp.pool import ProjectWorkerPool

    saved = ProjectWorkerPool._readonly_fact("claude", "plan")
    assert "mode=plan" in saved.mechanism
    assert "not yet confirmed" not in (saved.gap or "")
    assert saved.capability.value == "enforceable"

    unconfirmed = ProjectWorkerPool._readonly_fact("claude", "unconfirmed")
    assert "not yet confirmed" in unconfirmed.gap


def test_execution_closes_only_its_worker_connection(tmp_path, monkeypatch):
    from contextlib import AsyncExitStack

    from src.middlewares.acp import pool as pool_module

    class FakeWorker:
        def __init__(self, provider, command, cwd, **kwargs):
            self.process = SimpleNamespace(returncode=None)
            self._prompt_locks = {}
            self._refs = {}
            self.readonly_enforcement = kwargs.get("readonly_enforcement")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            self.process.returncode = 0

    monkeypatch.setattr(pool_module, "ACPWorker", FakeWorker)

    async def scenario():
        async with AsyncExitStack() as stack:
            pool = pool_module.ProjectWorkerPool(stack, [{
                "provider_id": "claude", "command": ["fake"], "grants": [],
            }])
            first = await pool.get_readonly("claude", str(tmp_path), mode_id="plan", session_key="worker-a")
            other = await pool.get_readonly("claude", str(tmp_path), mode_id="plan", session_key="worker-b")
            assert first is not other
            first._prompt_locks["session-a"] = asyncio.Lock()
            await first._prompt_locks["session-a"].acquire()
            with pytest.raises(ValueError, match="正在运行"):
                await pool.get_readonly("claude", str(tmp_path), mode_id="acceptEdits", session_key="worker-a")
            first._prompt_locks["session-a"].release()
            other._prompt_locks["session-b"] = asyncio.Lock()
            await other._prompt_locks["session-b"].acquire()
            ref = WorkerRef("claude", "session-a", "project", "task", "worker-a", "developer",
                            str(tmp_path), isolation="native-readonly", config_version=pool._versions["claude"])
            execution = await pool.get_execution(ref, "a" * 32)
            assert first.process.returncode == 0
            assert other.process.returncode is None
            assert execution.process.returncode is None
            with pytest.raises(ValueError, match="正在开发"):
                await pool.get_readonly("claude", str(tmp_path), mode_id="plan", session_key="worker-a")
            assert await pool.get_readonly("claude", str(tmp_path), mode_id="plan", session_key="worker-b") is other
            await pool.release_execution(ref)

    asyncio.run(scenario())
