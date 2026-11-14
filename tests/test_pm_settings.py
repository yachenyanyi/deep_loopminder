"""PM configuration migration, isolation and credential handling."""

import asyncio
import json

import pytest
from langgraph.store.memory import InMemoryStore

from src.middlewares.agent.model_catalog import ModelCatalog
from src.middlewares.agent.pm_settings import PMSettings, migrate_legacy
from src.middlewares.project_state.worker_modes import ProjectWorkerModes


def test_pm_settings_atomic_updates_preserve_unrelated_env_and_reject_invalid_json(tmp_path):
    async def scenario():
        (tmp_path / ".env").write_text("OTHER_AGENT_KEY=untouched\n", encoding="utf-8")
        settings = PMSettings(tmp_path)
        await settings.update(lambda value: value.update({"workspace": {"trace_enabled": False}}),
                              secrets={"PM_TEST_KEY": "secret with spaces"})
        await settings.put("runtime", "port", 2345)
        assert settings.read()["workspace"]["trace_enabled"] is False
        assert settings.read()["runtime"]["port"] == 2345
        assert settings.secret("PM_TEST_KEY") == "secret with spaces"
        assert "secret with spaces" not in settings.path.read_text(encoding="utf-8")
        assert "OTHER_AGENT_KEY=untouched" in settings.env_path.read_text(encoding="utf-8")
        settings.path.write_text("{broken", encoding="utf-8")
        with pytest.raises(ValueError, match="格式无效"):
            await settings.put("runtime", "port", 3000)
        assert settings.path.read_text(encoding="utf-8") == "{broken"
        assert settings.secret("PM_TEST_KEY") == "secret with spaces"

    asyncio.run(scenario())


def test_failed_json_replace_restores_previous_env(tmp_path, monkeypatch):
    from src.middlewares.agent import pm_settings

    async def scenario():
        settings = PMSettings(tmp_path)
        await settings.update(lambda value: value.update({"workspace": {"trace_enabled": True}}),
                              secrets={"PM_TEST_KEY": "old-secret"})
        before_json = settings.path.read_bytes()
        before_env = settings.env_path.read_bytes()
        replace = pm_settings.os.replace

        def fail_config(source, target):
            if str(target) == str(settings.path):
                raise OSError("simulated configuration failure")
            return replace(source, target)

        monkeypatch.setattr(pm_settings.os, "replace", fail_config)
        with pytest.raises(OSError):
            await settings.update(lambda value: value["workspace"].update({"trace_enabled": False}),
                                  secrets={"PM_TEST_KEY": "new-secret"})
        assert settings.path.read_bytes() == before_json
        assert settings.env_path.read_bytes() == before_env

    asyncio.run(scenario())


def test_settings_writers_share_a_lock_even_with_different_data_dirs(tmp_path):
    """PM processes updating the same file must not lose each other's settings."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    code = """
import asyncio,sys
from pathlib import Path
from src.middlewares.agent.pm_settings import PMSettings
async def main():
    settings=PMSettings(Path(sys.argv[1]))
    for _ in range(12):
        await settings.update(lambda value: value.setdefault('counts',{}).__setitem__(sys.argv[2],value.setdefault('counts',{}).get(sys.argv[2],0)+1))
asyncio.run(main())
"""
    processes = []
    try:
        for index in range(3):
            env = os.environ.copy()
            env["LOOPMINDER_DATA_DIR"] = str(tmp_path / f"data-{index}")
            processes.append(subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(index)],
                                              cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
        for process in processes:
            _, error = process.communicate(timeout=25)
            assert process.returncode == 0, error.decode("utf-8", errors="replace")
        assert PMSettings(tmp_path).read()["counts"] == {"0": 12, "1": 12, "2": 12}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_pm_model_and_mode_settings_use_json_and_env(tmp_path):
    async def scenario():
        store, settings = InMemoryStore(), PMSettings(tmp_path)
        catalog = ModelCatalog(store, settings=settings)
        model = await catalog.save("local", "http://127.0.0.1:8000/v1", token="model-secret",
                                   models=[{"id": "a", "enabled": True, "reasoning_levels": ["high"]}])
        await catalog.set_default("local", "a", "high")
        assert await catalog.get_credential("local", model) == "model-secret"
        assert (await catalog.get_default())["reasoning_effort"] == "high"
        assert not await store.asearch(("deep_loopminder", "model_endpoints"))
        with pytest.raises(ValueError, match="重新提供凭据"):
            await catalog.save("local", "http://127.0.0.1:8001/v1")
        await settings.update(lambda values: values["models"]["providers"]["local"].update(
            {"credential_env": "PM_MISSING_CREDENTIAL"}))
        changed = await catalog.save("local", "http://127.0.0.1:8001/v1")
        assert "credential_env" not in changed
        modes = ProjectWorkerModes(store, settings=settings)
        await modes.save("p1", "claude", "plan")
        await modes.save("p2", "claude", "default")
        assert (await modes.get("p1", "claude"))["mode_id"] == "plan"
        assert (await modes.get("p2", "claude"))["mode_id"] == "default"
        assert "model-secret" not in settings.path.read_text(encoding="utf-8")

    asyncio.run(scenario())


def test_worker_nonsecret_environment_does_not_corrupt_trajectory_redaction(tmp_path, monkeypatch):
    from src.middlewares.agent.pm_settings import (
        protect_runtime_config,
        resolve_runtime_config,
    )
    from src.middlewares.trajectory import middleware as trajectory

    async def scenario():
        settings = PMSettings(tmp_path)
        monkeypatch.setattr(trajectory, "_known_secrets", set())
        monkeypatch.setattr(trajectory, "_exact_secrets", set())
        monkeypatch.setenv("PM_OTHER_PROCESS_KEY", "unrelated-process-secret")
        assert settings.secret("PM_OTHER_PROCESS_KEY") is None
        worker, secrets = protect_runtime_config({
            "provider_id": "claude", "env": {"DISABLE_AUTOUPDATER": "1", "API_KEY": "actual-test-key",
                                               "CUSTOM_VALUE": "synthetic-private-value",
                                               "CUSTOM_CONFIG": '{"unusual_field":"nested-private-value"}'}
        }, "worker_field")
        await settings.update(lambda values: values.update({"worker": worker}), secrets=secrets)
        resolved = resolve_runtime_config(worker, settings)
        assert resolved["env"]["DISABLE_AUTOUPDATER"] == "1"
        assert "1" not in trajectory._known_secrets
        assert "actual-test-key" in trajectory._known_secrets
        assert trajectory.TrajectoryRecorder.clean_public({"run_id": "run-1"})["run_id"] == "run-1"
        assert trajectory.TrajectoryRecorder.clean_public({"output": "echo synthetic-private-value"}) == {
            "output": "echo [已脱敏]"
        }
        assert trajectory.TrajectoryRecorder.clean_public({"output": "1"}) == {"output": "[已脱敏]"}
        assert trajectory.TrajectoryRecorder.clean_public({"output": "nested-private-value"}) == {
            "output": "[已脱敏]"
        }

    asyncio.run(scenario())


def test_migrate_legacy_prefers_existing_values_and_keeps_secrets_out_of_json(tmp_path, monkeypatch):
    from src.middlewares.agent import model_catalog

    for name in ("ACP_WORKERS_JSON", "ACP_SUBAGENTS_JSON", "PM_CLEF_ACCOUNT_ID", "PM_CLEF_API_KEY",
                 "CLOUDFLARE_AUTH_TOKEN", "CLOUDFLARE_AI_API_KEY", "LANGGRAPH_POSTGRES_URI"):
        monkeypatch.delenv(name, raising=False)

    class Credentials:
        def __init__(self, prefix=""):
            self.prefix = prefix

        async def get(self, identity):
            return "legacy-secret" if self.prefix != "DeepLoopMinder/clef/" else "clef-secret"

    async def scenario():
        settings = PMSettings(tmp_path)
        settings.path.write_text(json.dumps({"workspace": {"trace_enabled": False}}), encoding="utf-8")
        settings.env_path.write_text("OTHER_AGENT_KEY=keep\nCLOUDFLARE_ACCOUNT_ID=abc123\n", encoding="utf-8")
        store = InMemoryStore()
        await store.aput(("deep_loopminder", "model_endpoints"), "local", {
            "provider_id": "local", "base_url": "http://127.0.0.1:8000/v1",
            "models": [{"id": "a", "enabled": True}],
        })
        await store.aput(("deep_loopminder", "pm_settings"), "workspace", {"trace_enabled": True})
        await store.aput(("deep_loopminder", "project_worker_modes"), "p1/claude", {
            "project_id": "p1", "provider_id": "claude", "mode_id": "plan"})
        await migrate_legacy(store, settings, workers={})
        assert settings.read()["workspace"]["trace_enabled"] is False
        assert settings.read()["projects"]["p1"]["worker_modes"]["claude"]["mode_id"] == "plan"
        assert await ModelCatalog(store, settings=settings).get_credential(
            "local", settings.read()["models"]["providers"]["local"]) == "legacy-secret"
        assert settings.secret("PM_CLEF_ACCOUNT_ID") == "abc123"
        assert settings.secret("PM_CLEF_API_KEY") == "clef-secret"
        assert "legacy-secret" not in settings.path.read_text(encoding="utf-8")
        assert "clef-secret" not in settings.path.read_text(encoding="utf-8")
        assert "OTHER_AGENT_KEY=keep" in settings.env_path.read_text(encoding="utf-8")
        assert await migrate_legacy(store, settings, workers={}) == []

    monkeypatch.setattr(model_catalog, "WindowsCredentials", Credentials)
    asyncio.run(scenario())


def test_optional_memory_does_not_remove_required_pm_guards():
    from src.deep_agents.agents.pm_agent.agent import (
        assemble_pm_user_middleware,
        default_pm_profile,
    )
    from src.deep_agents.agents.pm_agent.discussion import PMDiscussionMiddleware
    from src.middlewares.approval import HumanDecisionRequestMiddleware
    from src.middlewares.memory.pm_agent_memory import PMAgentMemoryMiddleware
    from src.middlewares.project_state import ProjectStateMiddleware

    stack = assemble_pm_user_middleware(default_pm_profile(memory_enabled=False), memory_enabled=False)
    assert not any(isinstance(item, PMAgentMemoryMiddleware) for item in stack)
    assert any(isinstance(item, PMDiscussionMiddleware) for item in stack)
    assert any(isinstance(item, HumanDecisionRequestMiddleware) for item in stack)
    assert any(isinstance(item, ProjectStateMiddleware) for item in stack)


def test_pm_trace_switch_applies_to_worker_records(tmp_path, monkeypatch):
    from src.middlewares.acp import graph
    from src.middlewares.agent import pm_settings
    from src.middlewares.trajectory import paths

    async def scenario():
        settings = PMSettings(tmp_path)
        monkeypatch.setenv("LOOPMINDER_PM_SETTINGS", "1")
        monkeypatch.setattr(pm_settings, "_ROOT", tmp_path)
        monkeypatch.setattr(paths, "trajectory_directory", lambda _: tmp_path / "trace")
        await settings.update(lambda values: values.update({
            "runtime": {"middleware": {"trajectory": False}}, "workspace": {"trace_enabled": True}}))
        assert graph.project_recorder("project") is None
        await settings.update(lambda values: values.update({
            "runtime": {"middleware": {"trajectory": True}}, "workspace": {"trace_enabled": False}}))
        assert graph.project_recorder("project") is None
        await settings.update(lambda values: values["workspace"].update({"trace_enabled": True}))
        assert graph.project_recorder("project") is not None

    asyncio.run(scenario())


def test_pm_business_and_acp_graph_do_not_import_the_workbench():
    """Keep presentation out of PM governance and the ACP execution bridge."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for relative in ("src/deep_agents/agents/pm_agent/collaboration.py", "src/middlewares/acp/graph.py"):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(name.name.startswith("src.workbench") for name in node.names), relative
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("src.workbench"), relative


def test_pm_initial_model_comes_from_pm_settings(tmp_path, monkeypatch):
    """A configured PM model must not require another Agent's legacy model settings."""
    from types import SimpleNamespace

    from src.deep_agents.agents.pm_agent import agent
    from src.middlewares.agent import pm_settings, user_configurable
    from src.middlewares.persistence import pm_runtime

    async def scenario():
        settings = PMSettings(tmp_path)
        await settings.update(lambda values: values.update({
            "models": {"default": {"provider_id": "p", "model_id": "m"}},
            "runtime": {"middleware": {"memory": False, "acp": False}},
        }))
        monkeypatch.setenv("LOOPMINDER_PM_SETTINGS", "1")
        monkeypatch.setattr(pm_settings, "_ROOT", tmp_path)
        monkeypatch.setattr(pm_runtime, "require_ready", lambda: None)
        store, selected_model = InMemoryStore(), object()

        async def memory_store():
            return SimpleNamespace(store=store)

        async def configured_model(catalog, selection):
            assert selection == {"provider_id": "p", "model_id": "m"}
            return selected_model

        monkeypatch.setattr(agent, "get_pm_memory_store", memory_store)
        monkeypatch.setattr(user_configurable, "configured_pm_model", configured_model)
        monkeypatch.setattr(agent, "create_agent", lambda model, **kwargs: (model, kwargs))
        model, result = await agent.pm_agent(studio=True)
        assert model is selected_model
        assert not any(type(item).__name__ == "PMAgentMemoryMiddleware" for item in result["middleware"])

    asyncio.run(scenario())


def test_workbench_pm_runtime_settings_keep_secrets_in_env(tmp_path, monkeypatch):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from src.middlewares.agent import pm_settings
    from src.workbench import app as workbench
    from src.workbench import context as workbench_context

    monkeypatch.setenv("LOOPMINDER_PM_SETTINGS", "1")
    monkeypatch.setattr(pm_settings, "_ROOT", tmp_path)
    monkeypatch.setattr(workbench_context, "_injected_store", InMemoryStore())
    client = TestClient(Starlette(routes=workbench.routes), base_url="http://127.0.0.1:2024",
                        client=("127.0.0.1", 50000))
    with client:
        response = client.post("/workbench/api/runtime-settings", json={
            "database_uri": "postgresql://user:db-secret@localhost:5432/pm",
            "middleware": {"memory": False, "trajectory": True, "acp": True},
            "acp_workers": [{"provider_id": "claude", "env": {"SECRET": "worker-secret"},
                             "approval": {"config": {"api_key": "approval-secret"}},
                             "command": ["node", "--token=command-secret", "--api-key", "separate-secret", "--verbose"]}],
        })
        assert response.status_code == 200
        payload = client.get("/workbench/api/runtime-settings").json()
        assert payload["middleware"]["memory"] is False
        assert payload["database"]["configured"] is True
        assert all(secret not in str(payload) for secret in ("db-secret", "worker-secret", "approval-secret", "command-secret", "separate-secret"))
        text = (tmp_path / "settings.json").read_text(encoding="utf-8")
        assert all(secret not in text for secret in ("db-secret", "worker-secret", "approval-secret", "command-secret", "separate-secret"))
        assert "db-secret" in (tmp_path / ".env").read_text(encoding="utf-8")
        assert "worker-secret" in (tmp_path / ".env").read_text(encoding="utf-8")
        assert "approval-secret" in (tmp_path / ".env").read_text(encoding="utf-8")
        assert "command-secret" in (tmp_path / ".env").read_text(encoding="utf-8")
        assert "separate-secret" in (tmp_path / ".env").read_text(encoding="utf-8")
        assert client.get("/workbench/api/worker-settings?provider_id=claude").json()["source"] == "custom"


def test_pm_setup_server_uses_isolated_registry_and_remains_configurable(tmp_path):
    """The real dev server must not share another Agent's runtime directory."""
    import os
    import socket
    import subprocess
    import sys
    import time
    import urllib.error
    import urllib.request
    from pathlib import Path

    (tmp_path / "settings.json").write_text("", encoding="utf-8")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    environment = os.environ.copy()
    environment.update(LOOPMINDER_PM_CONFIG_ROOT=str(tmp_path), PYTHONUTF8="1")
    environment.pop("LANGGRAPH_POSTGRES_URI", None)
    environment.pop("PM_POSTGRES_URI", None)
    root = Path(__file__).resolve().parents[1]
    log_path = tmp_path / "server.log"
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, str(root / "scripts/run_pm_server.py"), "--port", str(port)],
            cwd=root, env=environment, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            base = f"http://127.0.0.1:{port}"
            state = None
            for _ in range(100):
                if proc.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(base + "/workbench/api/runtime-settings", timeout=1) as response:
                        state = json.load(response)
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.2)
            if state is None:
                log.flush()
                pytest.fail("PM setup server did not start: " + log_path.read_text(encoding="utf-8", errors="replace")[-5000:])
            assert state["database"]["store_ready"] is False
            with urllib.request.urlopen(base + "/workbench") as response:
                assert response.status == 200
            with urllib.request.urlopen(base + "/workbench/assets/app.js") as response:
                assert response.status == 200
            with pytest.raises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(base + "/workbench/api/projects")
            assert rejected.value.code == 503
            body = json.dumps({"database_uri": "postgresql://tester:probe-secret@127.0.0.1:5432/missing"}).encode()
            request = urllib.request.Request(base + "/workbench/api/runtime-settings", data=body,
                                             headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request) as response:
                assert response.status == 200
            assert "probe-secret" not in (tmp_path / "settings.json").read_text(encoding="utf-8")
            assert "probe-secret" in (tmp_path / ".env").read_text(encoding="utf-8")
            assert (tmp_path / "workspace/pm-runtime/.langgraph_api").is_dir()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
