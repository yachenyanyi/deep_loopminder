"""Conversation ownership across project workbench endpoints."""

import asyncio
from types import SimpleNamespace

import httpx
from langgraph.store.memory import InMemoryStore
from starlette.applications import Starlette
from starlette.routing import Route

from src.middlewares.project_state.models import ProjectSnapshot
from src.middlewares.project_state.persistence import ProjectStore
from src.workbench import threads
from src.workbench.claims import ClaimWatcher
from src.workbench.context import THREADS
from src.workbench.worker_reviews import _owner_for_worker


def test_conversations_are_independent_and_bound_to_project(monkeypatch):
    async def scenario():
        store = InMemoryStore()
        await ProjectStore(store).save(ProjectSnapshot("p1", "first", (), ()))
        await ProjectStore(store).save(ProjectSnapshot("p2", "second", (), ()))
        saved = {}

        class OfficialThreads:
            async def create(self, *, thread_id, metadata, **_):
                saved[thread_id] = {"thread_id": thread_id, "metadata": metadata}
                return saved[thread_id]

            async def get(self, thread_id):
                return saved[thread_id]

        client = SimpleNamespace(threads=OfficialThreads())
        async def get_store():
            return store
        monkeypatch.setattr(threads, "get_store", get_store)
        monkeypatch.setattr(threads, "get_client", lambda **_: client)
        app = Starlette(routes=[Route("/conversations", threads.conversations, methods=["GET", "POST"])])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 3000)),
                                     base_url="http://127.0.0.1") as http:
            first = (await http.post("/conversations", json={"project_id": "p1", "title": "A"})).json()
            second = (await http.post("/conversations", json={"project_id": "p1", "title": "B"})).json()
            third = (await http.post("/conversations", json={"project_id": "p2"})).json()
            assert len({first["thread_id"], second["thread_id"], third["thread_id"]}) == 3
            listed = (await http.get("/conversations?project_id=p1")).json()["conversations"]
            assert {row["title"] for row in listed} == {"A", "B"}
            assert await threads.project_thread(store, client, "p1", first["thread_id"]) == first["thread_id"]
            try:
                await threads.project_thread(store, client, "p2", first["thread_id"])
            except ValueError:
                pass
            else:
                raise AssertionError("cross-project thread accepted")

    asyncio.run(scenario())


def test_missing_registered_thread_restores_only_verified_checkpoint(monkeypatch):
    """A missing registry entry cannot create an empty or foreign PM session."""
    import pytest
    from langgraph.checkpoint.base import CheckpointTuple

    from src.middlewares.persistence import pm_runtime

    async def scenario():
        store = InMemoryStore()
        await store.aput(THREADS, "thread", {"project_id": "project", "thread_id": "thread"})
        checkpoint = CheckpointTuple(
            config={"configurable": {"thread_id": "thread", "checkpoint_ns": "", "checkpoint_id": "checkpoint"}},
            checkpoint={"v": 4, "id": "checkpoint", "ts": "2026-11-15T00:00:00Z",
                        "channel_values": {}, "channel_versions": {}, "versions_seen": {}, "updated_channels": None},
            metadata={"source": "loop", "step": 1, "parents": {}},
        )
        observed = []

        class OfficialThreads:
            mismatch = None
            async def get(self, _thread_id):
                request = httpx.Request("GET", "http://localhost/threads/thread")
                raise httpx.HTTPStatusError("missing", request=request,
                                            response=httpx.Response(404, request=request))
            async def create(self, **kwargs):
                observed.append(kwargs)
                returned = {**kwargs["metadata"], "graph_id": kwargs["graph_id"]}
                result = {"thread_id": kwargs["thread_id"], "metadata": returned}
                if self.mismatch == "thread_id":
                    result["thread_id"] = "other"
                elif self.mismatch:
                    returned[self.mismatch] = "other"
                return result
        official = OfficialThreads()
        client = SimpleNamespace(threads=official)

        async def load(_thread_id):
            return checkpoint
        monkeypatch.setattr(pm_runtime, "get_pm_checkpoint", load)
        assert await threads.project_thread(store, client, "project", "thread") == "thread"
        assert observed[0]["if_exists"] == "do_nothing" and observed[0]["graph_id"] == "pm_agent"
        for field in ("thread_id", "graph_id", "loopminder_project_id", "loopminder_purpose"):
            official.mismatch = field
            with pytest.raises(ValueError, match="身份不匹配"):
                await threads.project_thread(store, client, "project", "thread")
        official.mismatch = None
        before = len(observed)
        original = checkpoint
        for bad in (None, original._replace(config={"configurable": {"thread_id": "other"}})):
            checkpoint = bad
            with pytest.raises(ValueError, match="检查点"):
                await threads.project_thread(store, client, "project", "thread")
        assert len(observed) == before
        checkpoint = original
        with pytest.raises(ValueError, match="不属于"):
            await threads.project_thread(store, client, "other", "thread")
        assert len(observed) == before

    asyncio.run(scenario())


def test_claim_watcher_uses_each_conversations_owner():
    async def scenario():
        store = InMemoryStore()
        for thread_id in ("first", "second"):
            await store.aput(THREADS, thread_id, {"project_id": "p", "thread_id": thread_id})
        watcher = ClaimWatcher(store)
        calls = []
        async def values(_client, _thread):
            return {"async_tasks": {}}
        async def claimable(_client, thread_id, _values):
            return [("task", {"run_id": thread_id}, "success")]
        async def wake(_client, project_id, thread_id, _entries, _names):
            calls.append((project_id, thread_id))
            return True
        watcher._thread_values, watcher._claimable, watcher._wake = values, claimable, wake
        assert set(await watcher.poll_once(None)) == {("first", "first"), ("second", "second")}
        assert set(calls) == {("p", "first"), ("p", "second")}

    asyncio.run(scenario())


def test_claim_watcher_retries_only_after_known_claim_finishes():
    """A failed claim can retry once; a running or unknown claim cannot."""
    async def scenario():
        statuses = {"claim-1": "running"}
        class Runs:
            async def get(self, thread_id, run_id):
                if run_id not in statuses:
                    raise RuntimeError("unavailable")
                return {"status": statuses[run_id]}
        client = SimpleNamespace(runs=Runs())
        watcher = ClaimWatcher()
        assert not await watcher._already_triggered(client, "owner", "worker-run")
        watcher._mark_triggered("worker-run", "claim-1")
        assert await watcher._already_triggered(client, "owner", "worker-run")
        statuses["claim-1"] = "error"
        assert not await watcher._already_triggered(client, "owner", "worker-run")
        watcher._mark_triggered("worker-run", "claim-2")
        statuses["claim-2"] = "error"
        assert await watcher._already_triggered(client, "owner", "worker-run")
        watcher._mark_triggered("unknown-worker", None)
        assert await watcher._already_triggered(client, "owner", "unknown-worker")

        async def failed_create(*args, **kwargs):
            raise TimeoutError("outcome unknown")
        client.runs.create = failed_create
        task = {"run_id": "uncertain", "agent_name": "worker"}
        assert not await watcher._wake(client, "project", "owner", [("worker", task, "success")], {})
        assert await watcher._already_triggered(client, "owner", "uncertain")

    asyncio.run(scenario())


def test_worker_review_owner_comes_from_matching_dispatch(monkeypatch):
    from src.workbench import worker_reviews
    async def scenario():
        store = InMemoryStore()
        async def get_store():
            return store
        monkeypatch.setattr(worker_reviews, "get_store", get_store)
        await store.aput(("deep_loopminder", "project_dispatch_intents", "p"), "task", {
            "phase": "execution", "worker_agent_id": "worker", "owner_thread": "conversation"})
        assert await _owner_for_worker({"project_id": "p", "task_id": "task", "thread_id": "worker"}) == "conversation"
        assert await _owner_for_worker({"project_id": "p", "task_id": "task", "thread_id": "other"}) is None
    asyncio.run(scenario())


def test_dispatch_reconciliation_requires_matching_owner_and_run(monkeypatch):
    """Reconciliation reads the saved claim and refuses another owner's checkpoint."""
    from src.middlewares.project_state.artifacts import content_hash
    from src.workbench import app as workbench

    async def scenario():
        store = InMemoryStore()
        await store.aput(("deep_loopminder", "project_dispatch_intents", "p", "history"),
                         "intent", {"claimed_run_id": "run"})
        async def get_store():
            return store
        class Runs:
            async def list(self, *_args, **_kwargs):
                return [{"run_id": "run", "status": "success"}]
        class Threads:
            owner = "conversation"
            async def get_history(self, *_args, **_kwargs):
                return [{"values": {"owner_thread_id": self.owner,
                        "request": {"pm_dispatch_id": "intent", "pm_phase": "execution", "message": "work"},
                        "worker_ref": {"project_id": "p", "task_id": "task", "thread_id": "worker"}}}]
        threads = Threads()
        monkeypatch.setattr(workbench, "get_store", get_store)
        monkeypatch.setattr(workbench, "get_client", lambda **_: SimpleNamespace(runs=Runs(), threads=threads))
        intent = {"worker_agent_id": "worker", "intent_id": "intent", "phase": "execution",
                  "owner_thread": "conversation", "message_digest": content_hash("work")}
        assert (await workbench._matching_dispatches("p", "task", intent))[0]["run_id"] == "run"
        threads.owner = "another-conversation"
        assert await workbench._matching_dispatches("p", "task", intent) == []

    asyncio.run(scenario())


def test_history_chat_and_decision_follow_selected_thread(monkeypatch):
    from src.workbench import app as workbench
    from src.workbench import context, decisions

    async def scenario():
        store = InMemoryStore()
        await ProjectStore(store).save(ProjectSnapshot("p", "goal", (), ()))
        records = {}
        calls = []

        class OfficialThreads:
            async def create(self, *, thread_id, metadata, **_):
                records[thread_id] = {"thread_id": thread_id, "metadata": metadata}
                return records[thread_id]

            async def get(self, thread_id):
                return records[thread_id]

            async def get_state(self, thread_id):
                return {"values": {"messages": [{"type": "ai", "content": thread_id}]}, "checkpoint": {}}

        class OfficialRuns:
            async def stream(self, thread_id, graph, **kwargs):
                calls.append((thread_id, graph, kwargs["config"]["configurable"]["project_id"]))
                yield SimpleNamespace(event="updates", data={"thread": thread_id})

        client = SimpleNamespace(threads=OfficialThreads(), runs=OfficialRuns())
        monkeypatch.setattr(context, "_injected_store", store)
        monkeypatch.setattr(workbench, "get_client", lambda **_: client)
        monkeypatch.setattr(decisions, "get_client", lambda **_: client)
        first = await threads.create_conversation(store, client, "p", "A")
        second = await threads.create_conversation(store, client, "p", "B")
        app = Starlette(routes=workbench.routes)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 3000)),
                                     base_url="http://127.0.0.1") as http:
            for row in (first, second):
                tid = row["thread_id"]
                history = await http.get("/workbench/api/history", params={"project_id": "p", "thread_id": tid})
                assert history.status_code == 200 and history.json()["messages"][0]["content"] == tid
                pending = await http.get("/workbench/api/pending-decision", params={"project_id": "p", "thread_id": tid})
                assert pending.status_code == 200 and not pending.json()["pending"]
                chat = await http.post("/workbench/api/chat", json={"project_id": "p", "thread_id": tid, "message": "hello"})
                assert chat.status_code == 200 and tid in chat.text
            missing = await http.post("/workbench/api/chat", json={"project_id": "p", "thread_id": "other", "message": "hello"})
            assert missing.status_code == 400
        assert calls == [(first["thread_id"], "pm_agent", "p"), (second["thread_id"], "pm_agent", "p")]

    asyncio.run(scenario())


def test_history_preserves_catalog_model_names_despite_redaction(monkeypatch):
    """A public catalog identifier must not become a redaction placeholder."""
    from src.middlewares.agent import user_configurable
    from src.workbench import app as workbench
    from src.workbench import context

    async def scenario():
        store = InMemoryStore()
        await ProjectStore(store).save(ProjectSnapshot("p", "goal", (), ()))
        await store.aput(THREADS, "thread", {"project_id": "p", "thread_id": "thread"})
        class Threads:
            async def get(self, thread_id):
                return {"thread_id": thread_id, "metadata": {"loopminder_project_id": "p",
                         "loopminder_purpose": "pm_workbench"}}
            async def get_state(self, _thread_id):
                return {"values": {"messages": [], "pm_model_selection": {"provider_id": "local",
                        "model_id": "public-model", "reasoning_effort": "medium"}}}
        class Catalog:
            async def list(self):
                return [{"provider_id": "local", "models": [{"id": "public-model", "reasoning_levels": ["medium"]}]}]
        monkeypatch.setattr(context, "_injected_store", store)
        monkeypatch.setattr(workbench, "get_client", lambda **_: SimpleNamespace(threads=Threads()))
        monkeypatch.setattr(user_configurable, "pm_model_catalog", lambda _store: Catalog())
        monkeypatch.setattr(workbench.TrajectoryRecorder, "clean_public",
                            lambda value: {**value, "model_id": "[redacted]"} if isinstance(value, dict) and "model_id" in value else value)
        app = Starlette(routes=workbench.routes)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 3000)),
                                     base_url="http://127.0.0.1") as client:
            response = await client.get("/workbench/api/history", params={"project_id": "p", "thread_id": "thread"})
            assert response.status_code == 200
            assert response.json()["model_selection"]["model_id"] == "public-model"

    asyncio.run(scenario())


def test_restored_run_observer_tracks_only_selected_conversation():
    """A reopened page refreshes finished work without touching a switched view."""
    import json
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the workbench observer")
    source = (Path(__file__).resolve().parents[1] / "src/workbench/app.js").read_text(encoding="utf-8")
    observer = source[source.index("async function watchRestoredRun"):source.index("async function loadProject(p, conversation)")]
    script = """
const vm = require('vm');
const assert = require('assert');
let scheduled = [], opened = 0, requests = 0;
const state = { viewVersion: 1, busy: false, busyThreads: new Set(), conversation: {thread_id: 'a'} };
const button = {disabled: false};
let result = {run: {run_id: 'r1', status: 'running'}};
const context = { state, $: () => button, api: async () => { requests++; return result; },
  setTimeout: fn => scheduled.push(fn), showError: message => { throw Error(message); },
  openConversation: async conversation => { assert.strictEqual(conversation.thread_id, 'a'); opened++; } };
vm.createContext(context);
vm.runInContext(OBSERVER, context);
(async () => {
  await context.watchRestoredRun('p', 'a', 1);
  assert(state.busy && button.disabled && scheduled.length === 1);
  result = {run: {run_id: 'r1', status: 'success'}};
  await scheduled.shift()();
  assert.strictEqual(opened, 1);
  assert(!state.busy);
  // A timer from an old conversation must not query or refresh the new one.
  state.viewVersion = 2;
  await context.watchRestoredRun('p', 'a', 1);
  assert.strictEqual(requests, 2);
  // A run started and completed by the claim watcher still refreshes history.
  state.viewVersion = 1;
  await context.watchRestoredRun('p', 'a', 1, {id: 'previous', active: false});
  assert.strictEqual(opened, 2);
  // A locally streamed run is not refreshed out from under its stream.
  state.busyThreads.add('a');
  await context.watchRestoredRun('p', 'a', 1, {id: 'previous', active: false});
  assert.strictEqual(opened, 2);
  state.busyThreads.delete('a');
  await scheduled.pop()();
  assert.strictEqual(opened, 3);
  // Switching views while the request is in flight discards its response.
  state.busy = false;
  context.api = async () => { state.viewVersion = 2; return {run: {run_id: 'r2', status: 'running'}}; };
  await context.watchRestoredRun('p', 'a', 1);
  assert(!state.busy);
  assert.strictEqual(opened, 3);
})().catch(error => { console.error(error); process.exitCode = 1; });
""".replace("OBSERVER", json.dumps(observer))
    subprocess.run([node], input=script, text=True, check=True, capture_output=True)
