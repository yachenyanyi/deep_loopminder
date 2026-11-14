"""Dedicated PM workbench assembly using the existing official ACP server."""

import asyncio
import logging
from contextlib import asynccontextmanager

from langgraph_sdk import get_client
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.deep_agents.agents.pm_agent.collaboration import (
    PMWorkerGovernance,
    recover_worker_executions,
)
from src.middlewares.acp import server as acp_server
from src.middlewares.persistence.pm_runtime import status
from src.workbench.app import routes
from src.workbench.claims import watch_claims
from src.workbench.context import get_store
from src.workbench.worker_reviews import (
    WorkerReview,
    pending_worker_reviews,
    submit_worker_review,
)

_LOGGER = logging.getLogger(__name__)


async def _watch_worker_recovery():
    """Inspect persisted executions at startup and while normal runs settle."""
    client = get_client(url=None)
    initial = True
    while True:
        try:
            await recover_worker_executions(await get_store(), client, include_completed=initial)
            initial = False
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  # transient Store/API failure must be retried
            _LOGGER.warning("Worker 执行状态核验失败，下一轮继续。", exc_info=True)
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app):
    """Start Worker connections when the PM store is available."""
    if not status()["store_ready"]:
        yield
        return
    app.state.worker_review = WorkerReview()
    app.state.pm_execution_review = app.state.worker_review.review
    async with acp_server.lifespan(app):
        claims_task = asyncio.create_task(watch_claims()) if app.state.acp_enabled else None
        recovery_task = asyncio.create_task(_watch_worker_recovery())
        try:
            yield
        finally:
            if claims_task is not None:
                claims_task.cancel()
                await asyncio.gather(claims_task, return_exceptions=True)
            recovery_task.cancel()
            await asyncio.gather(recovery_task, return_exceptions=True)
            app.state.worker_review.close()


def acp_worker():
    """Assemble the ACP bridge with PM collaboration rules."""
    return acp_server.acp_worker(governance=PMWorkerGovernance())


async def setup_only_when_unavailable(request, call_next):
    """Keep setup accessible without creating ephemeral projects."""
    allowed = {"/workbench", "/workbench/", "/workbench/assets/app.js", "/workbench/api/runtime-settings"}
    path = request.url.path
    if path.startswith("/workbench") and path not in allowed and not status()["store_ready"]:
        return JSONResponse({"error": "PM 数据库未连接；请先在设置中配置数据库并重启服务。"}, status_code=503)
    return await call_next(request)


app = Starlette(routes=[*routes,
                        Route("/workbench/api/worker-reviews", pending_worker_reviews, methods=["GET"]),
                        Route("/workbench/api/worker-reviews", submit_worker_review, methods=["POST"])], lifespan=lifespan,
                middleware=[Middleware(BaseHTTPMiddleware, dispatch=setup_only_when_unavailable)])
