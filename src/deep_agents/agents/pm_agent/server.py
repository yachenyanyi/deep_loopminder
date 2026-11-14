"""Dedicated PM workbench assembly using the existing official ACP server."""

from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.middlewares.acp import server as acp_server
from src.middlewares.agent.pm_settings import migrate_legacy
from src.middlewares.persistence.pm_runtime import status
from src.workbench.app import routes
from src.workbench.worker_reviews import (
    WorkerReview,
    pending_worker_reviews,
    submit_worker_review,
)


@asynccontextmanager
async def lifespan(app):
    """Migrate settings before starting Worker connections."""
    from langgraph_api.store import get_store

    store = await get_store()
    if not status()["store_ready"]:
        yield
        return
    await migrate_legacy(store)
    app.state.worker_review = WorkerReview()
    app.state.pm_execution_review = app.state.worker_review.review
    async with acp_server.lifespan(app):
        try:
            yield
        finally:
            app.state.worker_review.close()


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
