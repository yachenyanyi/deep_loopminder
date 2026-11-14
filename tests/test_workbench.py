"""The local workbench uses the project Store and rejects cross-origin calls."""

import asyncio

from langgraph.store.memory import InMemoryStore
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.workbench import app as workbench
from src.workbench import context as workbench_context
from src.workbench import settings_routes as workbench_settings


def test_local_project_creation_duplicate_and_origin(tmp_path, monkeypatch):
    store = InMemoryStore()
    monkeypatch.setattr(workbench_context, "_injected_store", store)
    app = Starlette(routes=workbench.routes)
    with TestClient(app, base_url="http://127.0.0.1:2024", client=("127.0.0.1", 50000)) as client:
        assert client.get("/workbench").status_code == 200
        response = client.post("/workbench/api/projects", json={"directory": str(tmp_path), "goal": "Demo"})
        assert response.status_code == 200
        project_id = response.json()["project_id"]
        assert client.get("/workbench/api/projects").json()[0]["project_id"] == project_id
        conflict = client.post("/workbench/api/projects", json={"directory": str(tmp_path), "goal": "Duplicate"})
        assert conflict.status_code == 409 and conflict.json()["project_id"] == project_id
        denied = client.post("/workbench/api/projects", json={"directory": str(tmp_path), "goal": "Cross origin"}, headers={"Origin": "https://other.example"})
        assert denied.status_code == 403
        assert client.get("/workbench", headers={"Host": "evil.example"}).status_code == 403
    with TestClient(app, base_url="http://127.0.0.1:2024", client=("203.0.113.1", 50000)) as remote:
        assert remote.get("/workbench").status_code == 403


def test_bind_legacy_project_and_set_default_model(tmp_path, monkeypatch):
    store = InMemoryStore()
    monkeypatch.setattr(workbench_context, "_injected_store", store)
    store.put(("deep_loopminder", "projects"), "old", {
        "schema_version": 1, "project_id": "old", "goal": "Old", "constraints": [], "tasks": []
    })
    with TestClient(Starlette(routes=workbench.routes), base_url="http://127.0.0.1:2024", client=("127.0.0.1", 50000)) as client:
        assert client.post("/workbench/api/projects/bind", json={"project_id": "missing", "directory": str(tmp_path)}).status_code == 404
        result = client.post("/workbench/api/projects/bind", json={"project_id": "old", "directory": str(tmp_path)})
        assert result.status_code == 200
        assert client.get("/workbench/api/projects").json()[0]["directory"] == str(tmp_path.resolve())
        result = client.post("/workbench/api/providers", json={"provider_id": "local", "base_url": "http://127.0.0.1:8000/v1", "models": [{"id": "m", "enabled": True, "reasoning_levels": ["low", "high"]}]})
        assert result.status_code == 200
        assert client.post("/workbench/api/default-model", json={"provider_id": "local", "model_id": "m", "reasoning_effort": "medium"}).status_code == 400
        assert client.post("/workbench/api/default-model", json={"provider_id": "local", "model_id": "m", "reasoning_effort": "high"}).status_code == 200
        assert client.get("/workbench/api/default-model").json()["reasoning_effort"] == "high"
        store.put(("deep_loopminder", "project_dispatch_intents", "old"), "t", {"status": "pending"})
        response = client.get("/workbench/api/dispatch-status?project_id=old&task_id=t")
        assert response.json()["intent"]["status"] == "pending"
        assert client.get("/workbench/api/dispatch-status?project_id=missing&task_id=t").status_code == 404


def test_failed_folder_binding_leaves_visible_recoverable_project(tmp_path, monkeypatch):
    store = InMemoryStore()
    monkeypatch.setattr(workbench_context, "_injected_store", store)
    original = workbench.ProjectFolders.bind

    async def failed_binding(self, project_id, directory):
        raise OSError("simulated interrupted binding")

    with TestClient(Starlette(routes=workbench.routes), base_url="http://127.0.0.1:2024", client=("127.0.0.1", 50000)) as client:
        monkeypatch.setattr(workbench.ProjectFolders, "bind", failed_binding)
        response = client.post("/workbench/api/projects", json={"directory": str(tmp_path), "goal": "Recoverable"})
        assert response.status_code == 503
        project_id = response.json()["project_id"]
        assert client.get("/workbench/api/projects").json() == [
            {"project_id": project_id, "goal": "Recoverable", "directory": None}
        ]
        monkeypatch.setattr(workbench.ProjectFolders, "bind", original)
        response = client.post("/workbench/api/projects/bind", json={"project_id": project_id, "directory": str(tmp_path)})
        assert response.status_code == 200


def test_workspace_settings_roundtrip_and_thresholds(tmp_path, monkeypatch):
    """Trace switch, compaction thresholds save; trace-only save keeps thresholds."""
    store = InMemoryStore()
    monkeypatch.setattr(workbench_context, "_injected_store", store)
    monkeypatch.delenv("PM_COMPACTION_TOKEN_THRESHOLD", raising=False)
    monkeypatch.delenv("PM_CONTEXT_TOKEN_LIMIT", raising=False)
    with TestClient(Starlette(routes=workbench.routes), base_url="http://127.0.0.1:2024", client=("127.0.0.1", 50000)) as client:
        current = client.get("/workbench/api/workspace-settings").json()
        assert current["trace_enabled"] is True
        assert current["compaction_token_threshold"] == 300000
        assert current["context_token_limit"] == 500000

        saved = client.post("/workbench/api/workspace-settings", json={
            "trace_enabled": False, "compaction_token_threshold": 120000, "context_token_limit": 240000})
        assert saved.status_code == 200
        saved_values = store.get(("deep_loopminder", "pm_settings"), "workspace").value
        assert saved_values == {"trace_enabled": False, "compaction_token_threshold": 120000,
                                "context_token_limit": 240000}
        assert client.get("/workbench/api/workspace-settings").json()["compaction_token_threshold"] == 120000

        # 只改轨迹开关不清掉已保存的阈值。
        assert client.post("/workbench/api/workspace-settings", json={"trace_enabled": True}).status_code == 200
        after = store.get(("deep_loopminder", "pm_settings"), "workspace").value
        assert after["trace_enabled"] is True
        assert after["compaction_token_threshold"] == 120000
        assert after["context_token_limit"] == 240000

        assert client.post("/workbench/api/workspace-settings", json={
            "trace_enabled": True, "compaction_token_threshold": 10}).status_code == 400
        assert client.post("/workbench/api/workspace-settings", json={}).status_code == 400


def test_project_status_reports_discussion_scope_declaration(tmp_path, monkeypatch):
    """A recorded conclusion's fingerprint-scope declaration reaches the API field."""
    import httpx

    from src.deep_agents.agents.pm_agent.discussion import DiscussionGate
    from src.middlewares.execution.security_policy import (
        EnforcementCapability,
        EnforcementFact,
    )
    from src.middlewares.project_state.folders import ProjectFolders
    from src.middlewares.project_state.models import ProjectSnapshot, Task, TaskStatus
    from src.middlewares.project_state.persistence import ProjectStore

    store = InMemoryStore()
    monkeypatch.setattr(workbench_context, "_injected_store", store)

    async def scenario():
        gate = DiscussionGate(store)
        await ProjectFolders(store).bind("project", str(tmp_path))
        (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
        brief = await gate.workflow.artifacts.create("project", "task", "task_brief", "# Goal\nDeliver")
        task = Task("task", "Feature", TaskStatus.READY, "developer", frozenset(), frozenset(),
                    ("Feature works",), artifact_refs=(brief["reference"],))
        await ProjectStore(store).save(ProjectSnapshot("project", "Goal", (), (task,)))
        enforcement = EnforcementFact("test_sandbox", EnforcementCapability.ENFORCEABLE,
                                      frozenset({"worker.readonly"}), mechanism="test fixture")
        await gate.workflow.record_start("project", "task", "parent", "worker", "run",
                                         phase="discussion", readonly_enforcement=enforcement)
        result_ref = await gate.workflow.record_result(
            "project", "parent",
            {"agent_id": "worker", "run_id": "run", "status": "success", "result": "Agreed."},
        )

        async def handler(request):
            return httpx.Response(200, json={"success": True,
                                             "result": {"answers": {"development_readiness": {"choice": "pass"}}}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            decision = await gate.evaluate("project", "task", owner_thread="parent",
                                           worker_result_ref=result_ref, pm_confirmation="Agreed",
                                           account_id="abc123", token="secret", client=client)
        return decision

    decision = asyncio.run(scenario())

    with TestClient(Starlette(routes=workbench.routes), base_url="http://127.0.0.1:2024", client=("127.0.0.1", 50000)) as client:
        payload = client.get("/workbench/api/project-status?project_id=project").json()
        discussion = payload["tasks"][0]["discussion"]
        assert discussion["content_digest_scope"] == decision["project_content_digest_scope"]
        assert discussion["content_digest_scope"].startswith("内容指纹")


def test_worker_modes_rejects_unadvertised_mode_with_live_session(tmp_path, monkeypatch):
    """有活动会话时只能保存适配器提供的模式；无会话仍可保存默认值。"""
    store = InMemoryStore()
    monkeypatch.setattr(workbench_context, "_injected_store", store)

    import asyncio

    from src.middlewares.project_state.models import ProjectSnapshot, Task, TaskStatus
    from src.middlewares.project_state.persistence import ProjectStore

    async def scenario():
        await workbench.ProjectFolders(store).bind("p", str(tmp_path))
        task = Task("t", "T", TaskStatus.READY, "developer", frozenset(), frozenset(), ("ok",))
        await ProjectStore(store).save(ProjectSnapshot("p", "Goal", (), (task,)))

    asyncio.run(scenario())

    async def fake_live(acp_server, provider, directory=None):
        return {"available": True, "modes": [{"id": "plan"}, {"id": "default"}],
                "current": "default", "reason": None}

    monkeypatch.setattr(workbench_settings, "_live_mode_state", fake_live)
    with TestClient(Starlette(routes=workbench.routes), base_url="http://127.0.0.1:2024", client=("127.0.0.1", 50000)) as client:
        ok = client.post("/workbench/api/worker-modes", json={"project_id": "p", "provider_id": "claude", "mode_id": "plan"})
        assert ok.status_code == 200 and ok.json()["saved_mode"] == "plan"
        bad = client.post("/workbench/api/worker-modes", json={"project_id": "p", "provider_id": "claude", "mode_id": "no-such-mode"})
        assert bad.status_code == 400 and "不被适配器提供" in bad.json()["error"]


def test_project_status_merges_adapter_reported_mode_from_run_record(tmp_path, monkeypatch):
    """派工竞态下决策记录缺回报字段时，展示层从 project_run_modes 权威记录合并。"""
    import asyncio

    store = InMemoryStore()
    monkeypatch.setattr(workbench_context, "_injected_store", store)

    async def scenario():
        from src.middlewares.project_state.models import (
            ProjectSnapshot,
            Task,
            TaskStatus,
        )
        from src.middlewares.project_state.persistence import ProjectStore

        await workbench.ProjectFolders(store).bind("p", str(tmp_path))
        task = Task("t", "T", TaskStatus.READY, "developer", frozenset(), frozenset(), ("ok",))
        await ProjectStore(store).save(ProjectSnapshot("p", "Goal", (), (task,)))
        await store.aput(("deep_loopminder", "project_run_modes", "p"), "run-x", {
            "run_id": "run-x", "requested_mode": "plan", "reported_mode": "plan",
            "reported_mode_source": "config_option_snapshot", "requested_mode_source": "default_plan",
            "confirmed_at": "now", "provider_id": "claude", "session_id": "s"}, index=False)

    asyncio.run(scenario())
    with TestClient(Starlette(routes=workbench.routes), base_url="http://127.0.0.1:2024", client=("127.0.0.1", 50000)) as client:
        merged = client.get("/workbench/api/project-status?project_id=p").json()
        assert merged["tasks"][0]["discussion"] is None  # 无商讨轮次时不合并
        assert workbench._merged_mode_record  # 助手可调用（签名在端点内使用）


def test_begin_dispatch_allows_redispatch_after_registry_404(tmp_path, monkeypatch):
    """官方注册表 404（如 dev 重启丢线程表）放行重派；其余错误仍拒绝。"""
    import asyncio

    import httpx

    from src.middlewares.execution.security_policy import (
        EnforcementCapability,
        EnforcementFact,
    )
    from src.middlewares.project_state.models import ProjectSnapshot, Task, TaskStatus
    from src.middlewares.project_state.persistence import ProjectStore
    from src.middlewares.project_state.workflow import (
        ProjectStateError,
        ProjectWorkflow,
    )

    store = InMemoryStore()
    monkeypatch.setattr(workbench_context, "_injected_store", store)

    async def scenario():
        await workbench.ProjectFolders(store).bind("p", str(tmp_path))
        brief = await ProjectWorkflow(store).artifacts.create("p", "t", "task_brief", "b")
        task = Task("t", "T", TaskStatus.READY, "developer", frozenset(), frozenset(), ("ok",),
                    artifact_refs=(brief["reference"],))
        await ProjectStore(store).save(ProjectSnapshot("p", "Goal", (), (task,)))
        workflow = ProjectWorkflow(store)

        async def fake_get(*_args, **_kwargs):
            raise httpx.HTTPStatusError("not found", request=httpx.Request("GET", "http://x"),
                                        response=httpx.Response(404, request=httpx.Request("GET", "http://x")))
        monkeypatch.setattr("src.middlewares.project_state.workflow.get_client", lambda url=None: type(
            "C", (), {"runs": type("R", (), {"get": staticmethod(fake_get)})})())
        intent = await workflow.begin_dispatch(
            "p", "t", owner_thread="o", operation="spawn_agent", phase="discussion",
            message_digest="d" * 64, discussion_question="q",
            readonly_enforcement=EnforcementFact(
                "m", EnforcementCapability.ENFORCEABLE, frozenset({"worker.readonly"}), mechanism="x"),
        )
        assert intent["status"] == "pending"

        async def fake_get_500(*_args, **_kwargs):
            raise httpx.HTTPStatusError("boom", request=httpx.Request("GET", "http://x"),
                                        response=httpx.Response(500, request=httpx.Request("GET", "http://x")))
        monkeypatch.setattr("src.middlewares.project_state.workflow.get_client", lambda url=None: type(
            "C", (), {"runs": type("R", (), {"get": staticmethod(fake_get_500)})})())
        # 上一意图已是 pending，不触发 confirmed 分支；先标记为 confirmed 再验证 500 路径。
        previous_ns = ("deep_loopminder", "project_dispatch_intents", "p")
        prev = await store.aget(previous_ns, "t")
        confirmed = {**prev.value, "agent_id": "agent-thread", "run_id": "run-1",
                     "claimed_run_id": "run-1", "status": "confirmed"}
        await store.aput(previous_ns, "t", confirmed, index=False)
        await store.aput((*previous_ns, "history"), confirmed["intent_id"], confirmed, index=False)
        try:
            await workflow.begin_dispatch(
                "p", "t", owner_thread="o", operation="spawn_agent", phase="discussion",
                message_digest="d" * 64, discussion_question="q2",
                readonly_enforcement=EnforcementFact(
                    "m", EnforcementCapability.ENFORCEABLE, frozenset({"worker.readonly"}), mechanism="x"),
            )
            raise AssertionError("500 应拒绝重派")
        except ProjectStateError as exc:
            assert exc.details["code"] == "dispatch_status_unknown"

    asyncio.run(scenario())
