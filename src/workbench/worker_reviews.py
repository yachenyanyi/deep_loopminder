"""Live PM Worker permission requests and their local workbench endpoints."""

import asyncio
import os
import re
from pathlib import Path
from uuid import uuid4

from starlette.responses import JSONResponse

from src.middlewares.trajectory.middleware import TrajectoryRecorder

from .context import forbidden, get_store, local


async def _owner_for_worker(ref):
    """Resolve the dispatch owner from the project's current execution intent."""
    project_id, task_id, worker_id = ref.get("project_id"), ref.get("task_id"), ref.get("thread_id")
    if not all(isinstance(value, str) and value for value in (project_id, task_id, worker_id)):
        return None
    store = await get_store()
    item = await store.aget(("deep_loopminder", "project_dispatch_intents", project_id), task_id)
    value = item.value if item else {}
    worker = value.get("worker_agent_id") or value.get("agent_id")
    if value.get("phase") == "execution" and value.get("discussion_required", True) is False and worker is None:
        active = await store.aget(("deep_loopminder", "project_worker_quarantine", project_id), worker_id)
        if active and active.value.get("intent_id") == value.get("intent_id"):
            worker = worker_id
    if (value.get("phase") != "execution" or worker != worker_id
            or not isinstance(value.get("owner_thread"), str) or not value["owner_thread"]):
        return None
    return value["owner_thread"]


class WorkerReview:
    """Keep a request only while its ACP connection is awaiting a decision."""

    def __init__(self, timeout=120, *, owner_for_worker=_owner_for_worker):
        """Create the in-process registry for this PM service lifecycle."""
        self.timeout = timeout
        self._owner_for_worker = owner_for_worker
        self._pending = {}

    async def review(self, request, decision):
        """Wait for a specific human decision; cancellation and timeout deny."""
        ref = request.context.get("worker_ref") or {}
        project_id = ref.get("project_id")
        if not isinstance(project_id, str) or not project_id or "approve" not in decision.allowed_decisions:
            return False
        owner = await self._owner_for_worker(ref)
        if not owner:
            return False
        if self._owner_for_worker is _owner_for_worker and await self._allow_cleared_development(request, ref):
            return True
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        visible = TrajectoryRecorder.clean_for_judgment({
            "request_id": request_id,
            "project_id": project_id,
            "thread_id": owner,
            "task_id": ref.get("task_id"),
            "provider_id": ref.get("provider_id"),
            "tool_name": request.tool_name,
            "tool_input": request.tool_input,
            "tool_call": request.context.get("tool_call"),
            "risk_level": decision.risk_level.value,
            "message": decision.interrupt_message,
            "timeout_seconds": self.timeout,
        })
        self._pending[request_id] = (project_id, owner, visible, future)
        try:
            async with asyncio.timeout(self.timeout):
                return await future
        except TimeoutError:
            return False
        finally:
            self._pending.pop(request_id, None)

    async def _allow_cleared_development(self, request, ref):
        """Allow ordinary Clef-approved development actions inside its project."""
        tool = request.tool_name
        if tool not in {"acp.read", "acp.edit", "acp.execute"}:
            return False
        if tool == "acp.execute":
            command = str((request.tool_input or {}).get("command") or "")
            if not command or re.search(
                    r"(?i)(\b(format|shutdown|reboot|rmdir|del|erase)\b|git\s+reset\s+--hard|\b(remove-item|stop-process)\b|powershell\s+[^\n]*-enc)",
                    command):
                return False
            working_directory = (request.tool_input or {}).get("cwd") or ref.get("cwd")
            return self._inside_project(working_directory, ref.get("cwd"))
        path = (request.tool_input or {}).get("file_path") or (request.tool_input or {}).get("path")
        return self._inside_project(path, ref.get("cwd"))

    @staticmethod
    def _inside_project(candidate, project_directory):
        if not isinstance(candidate, str) or not isinstance(project_directory, str):
            return False
        try:
            root = Path(project_directory).resolve()
            path = Path(candidate)
            if not path.is_absolute():
                path = root / path
            path = path.resolve()
            return os.path.commonpath((str(root), str(path))) == str(root)
        except (OSError, ValueError):
            return False

    def pending(self, project_id, thread_id):
        """Return only requests belonging to the displayed conversation."""
        return [item for project, owner, item, future in self._pending.values()
                if project == project_id and owner == thread_id and not future.done()]

    def submit(self, project_id, thread_id, request_id, approved):
        """Resolve one still-live request at most once."""
        item = self._pending.get(request_id)
        if item is None or item[0] != project_id or item[1] != thread_id or item[3].done():
            return False
        item[3].set_result(approved)
        return True

    def close(self):
        """Never leave outstanding requests approved when the service stops."""
        for _, _, _, future in self._pending.values():
            if not future.done():
                future.set_result(False)


async def pending_worker_reviews(request):
    """List live ACP permissions for the selected project."""
    if not local(request):
        return forbidden()
    project_id = request.query_params.get("project_id", "")
    thread_id = request.query_params.get("thread_id", "")
    if not project_id or not thread_id:
        return JSONResponse({"error": "请选择项目和会话"}, status_code=400)
    return JSONResponse({"requests": request.app.state.worker_review.pending(project_id, thread_id)})


async def submit_worker_review(request):
    """Approve or reject the exact request shown by the workbench."""
    if not local(request):
        return forbidden()
    try:
        data = await request.json()
        project_id, thread_id, request_id, approved = data.get("project_id"), data.get("thread_id"), data.get("request_id"), data.get("approved")
        if not isinstance(project_id, str) or not project_id or not isinstance(thread_id, str) or not thread_id or not isinstance(request_id, str) or type(approved) is not bool:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"error": "审批请求无效"}, status_code=400)
    if not request.app.state.worker_review.submit(project_id, thread_id, request_id, approved):
        return JSONResponse({"error": "该请求已结束、过期或不属于当前项目，请刷新。"}, status_code=409)
    return JSONResponse({"accepted": True})
