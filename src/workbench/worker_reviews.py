"""Live PM Worker permission requests and their local workbench endpoints."""

import asyncio
from uuid import uuid4

from starlette.responses import JSONResponse

from src.middlewares.trajectory.middleware import TrajectoryRecorder

from .context import forbidden, local


class WorkerReview:
    """Keep a request only while its ACP connection is awaiting a decision."""

    def __init__(self, timeout=120):
        """Create the in-process registry for this PM service lifecycle."""
        self.timeout = timeout
        self._pending = {}

    async def review(self, request, decision):
        """Wait for a specific human decision; cancellation and timeout deny."""
        ref = request.context.get("worker_ref") or {}
        project_id = ref.get("project_id")
        if not isinstance(project_id, str) or not project_id or "approve" not in decision.allowed_decisions:
            return False
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        visible = TrajectoryRecorder.clean_for_judgment({
            "request_id": request_id,
            "project_id": project_id,
            "task_id": ref.get("task_id"),
            "provider_id": ref.get("provider_id"),
            "tool_name": request.tool_name,
            "tool_input": request.tool_input,
            "tool_call": request.context.get("tool_call"),
            "risk_level": decision.risk_level.value,
            "message": decision.interrupt_message,
            "timeout_seconds": self.timeout,
        })
        self._pending[request_id] = (project_id, visible, future)
        try:
            async with asyncio.timeout(self.timeout):
                return await future
        except TimeoutError:
            return False
        finally:
            self._pending.pop(request_id, None)

    def pending(self, project_id):
        """Return only requests belonging to the displayed project."""
        return [item for project, item, future in self._pending.values()
                if project == project_id and not future.done()]

    def submit(self, project_id, request_id, approved):
        """Resolve one still-live request at most once."""
        item = self._pending.get(request_id)
        if item is None or item[0] != project_id or item[2].done():
            return False
        item[2].set_result(approved)
        return True

    def close(self):
        """Never leave outstanding requests approved when the service stops."""
        for _, _, future in self._pending.values():
            if not future.done():
                future.set_result(False)


async def pending_worker_reviews(request):
    """List live ACP permissions for the selected project."""
    if not local(request):
        return forbidden()
    project_id = request.query_params.get("project_id", "")
    if not project_id:
        return JSONResponse({"error": "请选择项目"}, status_code=400)
    return JSONResponse({"requests": request.app.state.worker_review.pending(project_id)})


async def submit_worker_review(request):
    """Approve or reject the exact request shown by the workbench."""
    if not local(request):
        return forbidden()
    try:
        data = await request.json()
        project_id, request_id, approved = data.get("project_id"), data.get("request_id"), data.get("approved")
        if not isinstance(project_id, str) or not project_id or not isinstance(request_id, str) or type(approved) is not bool:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"error": "审批请求无效"}, status_code=400)
    if not request.app.state.worker_review.submit(project_id, request_id, approved):
        return JSONResponse({"error": "该请求已结束、过期或不属于当前项目，请刷新。"}, status_code=409)
    return JSONResponse({"accepted": True})
