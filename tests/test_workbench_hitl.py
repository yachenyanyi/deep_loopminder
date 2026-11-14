"""Human decisions surface from the official interrupt and resume the original run.

The resume path is the official HITL mechanism: read the pending interrupt from
the thread state, answer it with {decisions: [{type: "respond", message}]} on the
same thread, and refuse honestly when the checkpoint no longer exists (dev-mode
restarts drop it). Plain chat must never be turned into a resume.
"""

import tempfile

import httpx
import pytest
from langgraph.store.memory import InMemoryStore
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.workbench import app as workbench
from src.workbench import context as workbench_context
from src.workbench import decisions as workbench_decisions

PROJECT = "p-hitl"
THREAD = "thread-hitl-1"

def hitl_interrupt(question="是否允许删除临时文件？", allowed=("respond",), interrupt_id="int-slot-1", checkpoint_id="ckpt-1"):
    value = {
        "action_requests": [
            {"name": "request_human_decision", "args": {"question": question, "context": "项目背景"}, "description": "pending approval"}
        ],
        "review_configs": [{"action_name": "request_human_decision", "allowed_decisions": list(allowed)}],
    }
    return {
        "values": {"messages": []},
        "checkpoint": {"checkpoint_id": checkpoint_id, "thread_id": THREAD},
        "tasks": [{"id": "task-1", "name": "model", "interrupts": [{"value": value, "id": interrupt_id}]}],
    }


def decision_payload(client, project_id, threads, answers, *, state=None, **overrides):
    if state is not None:
        threads.state = state
    shown = client.get(f"/workbench/api/pending-decision?project_id={project_id}").json()
    body = {"project_id": project_id, "answers": answers, "fingerprint": shown["fingerprint"]}
    if shown.get("interrupt_id"):
        body["interrupt_id"] = shown["interrupt_id"]
    if shown.get("checkpoint_id"):
        body["checkpoint_id"] = shown["checkpoint_id"]
    body.update(overrides)
    return client.post("/workbench/api/submit-decision", json=body)


class FakeRuns:
    def __init__(self):
        self.created = []

    async def create(self, thread_id, assistant_id, **kwargs):
        self.created.append({"thread_id": thread_id, "assistant_id": assistant_id, **kwargs})
        return {"run_id": "run-resume-1"}


class FakeThreads:
    def __init__(self, state=None):
        self.state = state
        self.state_error = None
        self.project_id = PROJECT

    async def get(self, thread_id):
        return {"thread_id": thread_id, "metadata": {"loopminder_project_id": self.project_id, "loopminder_purpose": "pm_workbench"}}

    async def get_state(self, thread_id):
        if self.state_error is not None:
            raise self.state_error
        return self.state


class FakeClient:
    def __init__(self, threads, runs):
        self.threads = threads
        self.runs = runs


@pytest.fixture
def workbench_client(monkeypatch):
    store = InMemoryStore()
    monkeypatch.setattr(workbench_context, "_injected_store", store)
    runs = FakeRuns()
    threads = FakeThreads()
    monkeypatch.setattr(workbench, "get_client", lambda url=None: FakeClient(threads, runs))
    monkeypatch.setattr(workbench_decisions, "get_client", lambda url=None: FakeClient(threads, runs))
    app = Starlette(routes=workbench.routes)
    with TestClient(app, base_url="http://127.0.0.1:2024", client=("127.0.0.1", 50000)) as client:
        with tempfile.TemporaryDirectory() as directory:
            response = client.post("/workbench/api/projects", json={"directory": directory, "goal": "HITL"})
            assert response.status_code == 200, response.text
            project_id = response.json()["project_id"]
        threads.project_id = project_id
        store.put(("deep_loopminder", "workbench_threads"), project_id,
                  {"thread_id": THREAD, "identity_version": 1}, index=False)
        yield client, store, threads, runs, project_id


def test_pending_decision_surfaces_official_interrupt(workbench_client):
    client, store, threads, runs, project_id = workbench_client
    threads.state = hitl_interrupt(interrupt_id="int-slot-1", checkpoint_id="ckpt-1")
    data = client.get(f"/workbench/api/pending-decision?project_id={project_id}").json()
    assert data["pending"] is True and data["answerable"] is True
    assert data["decisions"][0]["name"] == "request_human_decision"
    assert data["decisions"][0]["args"]["question"] == "是否允许删除临时文件？"
    assert data["decisions"][0]["allowed_decisions"] == ["respond"]
    assert isinstance(data["fingerprint"], str) and data["fingerprint"]
    # The occurrence identifiers the page displays and submit must echo.
    assert data["interrupt_id"] == "int-slot-1"
    assert data["checkpoint_id"] == "ckpt-1"
    assert data["binding_available"] is True


def test_submit_decision_resumes_with_official_respond_payload(workbench_client):
    client, store, threads, runs, project_id = workbench_client
    threads.state = hitl_interrupt()
    response = decision_payload(client, project_id, threads, ["允许，但先备份。"])
    assert response.status_code == 200, response.text
    assert response.json()["resumed"] is True
    call = runs.created[0]
    assert call["thread_id"] == THREAD and call["assistant_id"] == "pm_agent"
    assert call["command"] == {"resume": {"decisions": [
        {"type": "respond", "message": "允许，但先备份。"},
    ]}}
    assert call["config"]["configurable"]["project_id"] == project_id
    assert "input" not in call


def test_model_options_land_in_configurable_like_chat(workbench_client):
    """Chat puts model options in config.configurable; resume must do the same
    or the official server silently ignores them."""
    client, store, threads, runs, project_id = workbench_client
    threads.state = hitl_interrupt()
    response = decision_payload(client, project_id, threads, ["允许"],
                                model_provider="easycli", model_name="gpt-6-luna", reasoning_effort="high")
    assert response.status_code == 200, response.text
    configurable = runs.created[0]["config"]["configurable"]
    assert configurable["model_provider"] == "easycli"
    assert configurable["model_name"] == "gpt-6-luna"
    assert configurable["reasoning_effort"] == "high"
    assert "model_provider" not in {k: v for k, v in runs.created[0]["config"].items() if k != "configurable"}


def test_submit_binds_to_the_shown_interrupt_fingerprint(workbench_client):
    client, store, threads, runs, project_id = workbench_client
    threads.state = hitl_interrupt()
    missing = client.post("/workbench/api/submit-decision", json={
        "project_id": project_id, "answers": ["允许"],
    })
    assert missing.status_code == 400
    assert "重新打开项目" in missing.json()["error"]
    stale = client.post("/workbench/api/submit-decision", json={
        "project_id": project_id, "answers": ["允许"], "fingerprint": "0" * 64,
    })
    assert stale.status_code == 409
    assert "不一致" in stale.json()["error"] and "重新打开项目" in stale.json()["error"]
    assert not runs.created
    # The exact binding shown by the pending endpoint is accepted.
    ok = decision_payload(client, project_id, threads, ["允许"])
    assert ok.status_code == 200 and runs.created


def test_same_question_new_occurrence_is_refused(workbench_client):
    """Content fingerprint alone cannot tell one occurrence from another: the
    official interrupt id is a slot identity that repeats. The page-bound
    checkpoint/interrupt identifiers must catch a re-asked identical question,
    or a stale answer would silently resume the wrong interrupt."""
    client, store, threads, runs, project_id = workbench_client
    threads.state = hitl_interrupt(interrupt_id="int-slot-1", checkpoint_id="ckpt-1")
    shown = client.get(f"/workbench/api/pending-decision?project_id={project_id}").json()
    assert shown["interrupt_id"] == "int-slot-1" and shown["checkpoint_id"] == "ckpt-1"
    # The PM re-asks the exact same question (same content, same slot id —
    # that is how the official id behaves across occurrences); only the
    # checkpoint differs because it is a new occurrence.
    threads.state = hitl_interrupt(interrupt_id="int-slot-1", checkpoint_id="ckpt-2")
    refused = client.post("/workbench/api/submit-decision", json={
        "project_id": project_id, "answers": ["旧的答复"], "fingerprint": shown["fingerprint"],
        "interrupt_id": shown["interrupt_id"], "checkpoint_id": shown["checkpoint_id"],
    })
    assert refused.status_code == 409, refused.text
    assert "已变化" in refused.json()["error"] and "重新打开项目" in refused.json()["error"]
    assert not runs.created
    # Answering the current occurrence with fresh identifiers resumes.
    fresh = client.get(f"/workbench/api/pending-decision?project_id={project_id}").json()
    ok = client.post("/workbench/api/submit-decision", json={
        "project_id": project_id, "answers": ["新的答复"], "fingerprint": fresh["fingerprint"],
        "interrupt_id": fresh["interrupt_id"], "checkpoint_id": fresh["checkpoint_id"],
    })
    assert ok.status_code == 200 and runs.created


def test_missing_official_identifiers_are_reported_not_guessed(workbench_client):
    """Older SDK shapes may omit the interrupt id and checkpoint; the route
    must say the binding is unavailable instead of accepting a guess."""
    client, store, threads, runs, project_id = workbench_client
    threads.state = hitl_interrupt(interrupt_id="int-slot-1", checkpoint_id=None)
    data = client.get(f"/workbench/api/pending-decision?project_id={project_id}").json()
    assert data["interrupt_id"] == "int-slot-1" and data["checkpoint_id"] is None
    assert data["binding_available"] is False
    refused = client.post("/workbench/api/submit-decision", json={
        "project_id": project_id, "answers": ["允许"], "fingerprint": data["fingerprint"],
        "interrupt_id": data["interrupt_id"], "checkpoint_id": None,
    })
    assert refused.status_code == 409
    assert "未提供可核对的中断或检查点标识" in refused.json()["error"]
    assert not runs.created


def test_plain_chat_text_is_never_a_resume(workbench_client):
    client, store, threads, runs, project_id = workbench_client
    threads.state = hitl_interrupt()
    wrong_shape = client.post("/workbench/api/submit-decision", json={
        "project_id": project_id, "message": "继续吧",
    })
    assert wrong_shape.status_code == 400
    assert not runs.created
    # Even with a well-formed answer there must be nothing to resume.
    threads.state = {"values": {"messages": []}, "tasks": []}
    missing = client.post("/workbench/api/submit-decision", json={
        "project_id": project_id, "answers": ["继续吧"],
    })
    assert missing.status_code == 409
    assert "没有待处理的人工决策" in missing.json()["error"]
    assert not runs.created


def test_answer_count_and_allowed_decisions_are_checked(workbench_client):
    client, store, threads, runs, project_id = workbench_client
    threads.state = hitl_interrupt()
    too_many = decision_payload(client, project_id, threads, ["a", "b"])
    assert too_many.status_code == 400
    threads.state = hitl_interrupt(allowed=("approve",))
    not_respond = decision_payload(client, project_id, threads, ["a"])
    assert not_respond.status_code == 400
    assert "不接受文字答复" in not_respond.json()["error"]
    assert not runs.created


def test_unrecognized_interrupt_is_pending_but_not_answerable(workbench_client):
    client, store, threads, runs, project_id = workbench_client
    threads.state = {"values": {"messages": []}, "checkpoint": {"checkpoint_id": "c1"}, "tasks": [
        {"id": "task-1", "name": "node", "interrupts": [{"value": {"custom": "shape"}, "id": "int-9"}]}
    ]}
    data = client.get(f"/workbench/api/pending-decision?project_id={project_id}").json()
    assert data["pending"] is True and data["answerable"] is False and data["decisions"] == []
    refused = client.post("/workbench/api/submit-decision", json={
        "project_id": project_id, "answers": ["x"],
    })
    assert refused.status_code == 409
    assert not runs.created


def test_lost_checkpoint_is_reported_not_faked(workbench_client):
    client, store, threads, runs, project_id = workbench_client
    threads.state_error = httpx.HTTPStatusError(
        "not found", request=httpx.Request("GET", "http://test"), response=httpx.Response(404)
    )
    seen = client.get(f"/workbench/api/pending-decision?project_id={project_id}")
    assert seen.status_code == 409
    assert "无法恢复" in seen.json()["error"]
    resumed = client.post("/workbench/api/submit-decision", json={
        "project_id": project_id, "answers": ["允许"],
    })
    assert resumed.status_code == 409
    assert "无法恢复" in resumed.json()["error"]
    assert not runs.created


def test_empty_snapshot_after_dev_restart_is_checkpoint_lost(workbench_client):
    """After a dev-server restart get_state returns 200 with an empty snapshot
    (checkpoint: null), not 404 — it must still be reported as a lost session."""
    client, store, threads, runs, project_id = workbench_client
    threads.state = {"values": {}, "next": [], "checkpoint": None, "tasks": [], "interrupts": []}
    seen = client.get(f"/workbench/api/pending-decision?project_id={project_id}")
    assert seen.status_code == 409
    assert "无法恢复" in seen.json()["error"]
    resumed = client.post("/workbench/api/submit-decision", json={
        "project_id": project_id, "answers": ["允许"],
    })
    assert resumed.status_code == 409
    assert "无法恢复" in resumed.json()["error"]
    assert not runs.created
    client, store, threads, runs, project_id = workbench_client

    async def gone(thread_id):
        raise httpx.HTTPStatusError(
            "not found", request=httpx.Request("GET", "http://test"), response=httpx.Response(404)
        )

    threads.get = gone
    seen = client.get(f"/workbench/api/pending-decision?project_id={project_id}")
    assert seen.status_code == 409
    assert "官方服务中不存在" in seen.json()["error"]
    assert not runs.created
