"""Exercise the live ACP review through the PM workbench endpoints."""

import asyncio
from types import SimpleNamespace

import httpx
from starlette.applications import Starlette
from starlette.routing import Route

from src.middlewares.acp.control import _Client
from src.middlewares.acp.correlation import WorkerRef
from src.middlewares.approval.decision import ApprovalDecision, RiskLevel
from src.workbench.worker_reviews import (
    WorkerReview,
    pending_worker_reviews,
    submit_worker_review,
)


def test_acp_permission_requires_matching_live_human_decision():
    async def scenario():
        registry = WorkerReview(timeout=2)
        app = Starlette(routes=[Route("/reviews", pending_worker_reviews, methods=["GET"]),
                                Route("/reviews", submit_worker_review, methods=["POST"])])
        app.state.worker_review = registry
        ref = WorkerRef("claude", "session", "project", "task", "thread", "developer", "C:/project")

        class Policy:
            calls = 0
            blocked = False

            async def aevaluate(self, request):
                self.calls += 1
                if self.blocked:
                    return ApprovalDecision.blocked()
                return ApprovalDecision.needs_approval(RiskLevel.MEDIUM, "review edit")

        policy = Policy()
        worker = SimpleNamespace(_refs={"session": ref}, approval=policy, review=registry.review,
                                 _authorize=lambda capability, ref: None)
        call = SimpleNamespace(kind="edit", raw_input={"path": "file.py"},
                               model_dump=lambda **kwargs: {"title": "Edit file.py"})
        options = [SimpleNamespace(kind="allow_once", option_id="once")]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40000)),
                                     base_url="http://127.0.0.1") as client:
            waiting = asyncio.create_task(_Client(worker).request_permission("session", call, options))
            await asyncio.sleep(0)
            rows = (await client.get("/reviews?project_id=project")).json()["requests"]
            assert len(rows) == 1 and rows[0]["tool_input"] == {"path": "file.py"}
            request_id = rows[0]["request_id"]
            wrong = await client.post("/reviews", json={"project_id": "other", "request_id": request_id, "approved": True})
            assert wrong.status_code == 409 and not waiting.done()
            good = await client.post("/reviews", json={"project_id": "project", "request_id": request_id, "approved": True})
            assert good.status_code == 200
            assert (await waiting).outcome.option_id == "once"
            assert policy.calls == 2
            assert (await client.post("/reviews", json={"project_id": "project", "request_id": request_id,
                                                        "approved": True})).status_code == 409

            waiting = asyncio.create_task(_Client(worker).request_permission("session", call, options))
            await asyncio.sleep(0)
            request_id = (await client.get("/reviews?project_id=project")).json()["requests"][0]["request_id"]
            policy.blocked = True
            await client.post("/reviews", json={"project_id": "project", "request_id": request_id, "approved": True})
            assert (await waiting).outcome.outcome == "cancelled"

    asyncio.run(scenario())


def test_worker_review_times_out_and_shutdown_denies():
    from src.middlewares.approval.request import ApprovalRequest

    async def scenario():
        request = ApprovalRequest("acp.edit", {}, context={"worker_ref": {"project_id": "p"}})
        decision = ApprovalDecision.needs_approval(RiskLevel.MEDIUM, "review")
        registry = WorkerReview(timeout=0.01)
        assert not await registry.review(request, decision)
        assert not registry.pending("p")
        waiting = asyncio.create_task(registry.review(request, decision))
        await asyncio.sleep(0)
        registry.close()
        assert not await waiting
        assert not registry.pending("p")

    asyncio.run(scenario())
