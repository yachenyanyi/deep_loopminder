"""The local workbench uses the project Store and rejects cross-origin calls."""

from langgraph.store.memory import InMemoryStore
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.workbench import app as workbench


def test_local_project_creation_duplicate_and_origin(tmp_path, monkeypatch):
    store = InMemoryStore()
    monkeypatch.setattr(workbench, "get_store", lambda: store)
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
    monkeypatch.setattr(workbench, "get_store", lambda: store)
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
    monkeypatch.setattr(workbench, "get_store", lambda: store)
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
