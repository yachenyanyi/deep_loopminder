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
