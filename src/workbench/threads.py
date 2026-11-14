"""Project-owned PM conversations backed by official LangGraph threads."""

import asyncio
import uuid
from datetime import UTC

import httpx
from langgraph_sdk import get_client
from starlette.responses import JSONResponse

from src.middlewares.project_state.persistence import ProjectStore

from .context import THREADS, forbidden, get_store, local


async def list_conversations(store, project_id):
    """List conversations without activating or modifying their threads."""
    found, offset = [], 0
    while True:
        page = await store.asearch(THREADS, filter={"project_id": project_id}, limit=100, offset=offset)
        found.extend(dict(item.value) for item in page)
        if len(page) < 100:
            return sorted(found, key=lambda value: value.get("created_at", ""))
        offset += len(page)


async def create_conversation(store, client, project_id, title=None):
    """Create an official thread and bind it to one project."""
    from datetime import datetime

    if title is not None and (not isinstance(title, str) or len(title.strip()) > 120):
        raise ValueError("会话名称无效")
    thread_id = str(uuid.uuid4())
    metadata = {"loopminder_project_id": project_id, "loopminder_purpose": "pm_workbench"}
    async with asyncio.timeout(15):
        thread = await client.threads.create(thread_id=thread_id, if_exists="do_nothing", metadata=metadata)
    returned_metadata = thread.get("metadata") or {}
    if thread.get("thread_id") != thread_id or any(returned_metadata.get(k) != v for k, v in metadata.items()):
        raise ValueError("官方会话身份不匹配")
    record = {"thread_id": thread_id, "project_id": project_id, "title": (title or "").strip() or "新会话",
              "created_at": datetime.now(UTC).isoformat()}
    await store.aput(THREADS, thread_id, record, index=False)
    return record


async def project_thread(store, client, project_id, thread_id):
    """Validate that an existing official thread belongs to this project."""
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("请选择会话")
    item = await store.aget(THREADS, thread_id)
    if item is None or item.value.get("project_id") != project_id or item.value.get("thread_id") != thread_id:
        raise ValueError("会话不存在或不属于当前项目")
    try:
        async with asyncio.timeout(15):
            thread = await client.threads.get(thread_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return await _restore_registered_thread(client, project_id, thread_id)
        raise ValueError("无法核对会话，请检查官方服务。") from exc
    except (httpx.HTTPError, TimeoutError) as exc:
        raise ValueError("无法核对会话，请检查官方服务。") from exc
    metadata = thread.get("metadata") or {}
    if (thread.get("thread_id") != thread_id or metadata.get("loopminder_project_id") != project_id
            or metadata.get("loopminder_purpose") != "pm_workbench"):
        raise ValueError("官方会话与项目不匹配")
    return thread_id


async def _restore_registered_thread(client, project_id, thread_id):
    """Restore only a bound PM thread with an independently verified checkpoint."""
    from src.middlewares.persistence.pm_runtime import get_pm_checkpoint

    try:
        async with asyncio.timeout(15):
            checkpoint = await get_pm_checkpoint(thread_id)
    except Exception as exc:
        raise ValueError("无法读取 PM 检查点，未恢复会话。") from exc
    expected = {"thread_id": thread_id, "graph_id": "pm_agent",
                "loopminder_project_id": project_id, "loopminder_purpose": "pm_workbench"}
    metadata = checkpoint.metadata if checkpoint is not None else None
    config = checkpoint.config if checkpoint is not None else None
    configurable = config.get("configurable") if isinstance(config, dict) else None
    if (not isinstance(metadata, dict) or not isinstance(config, dict)
            or any(metadata.get(key) != value for key, value in expected.items())
            or not isinstance(configurable, dict) or configurable.get("thread_id") != thread_id):
        raise ValueError("找不到与项目匹配的 PM 检查点，未恢复会话。")
    try:
        async with asyncio.timeout(15):
            thread = await client.threads.create(
                thread_id=thread_id, if_exists="do_nothing", graph_id="pm_agent",
                metadata={"loopminder_project_id": project_id, "loopminder_purpose": "pm_workbench"},
            )
    except (httpx.HTTPError, TimeoutError) as exc:
        raise ValueError("无法恢复官方会话，请检查服务；没有重试旧 Run。") from exc
    actual = thread.get("metadata") if isinstance(thread, dict) else None
    if (not isinstance(actual, dict) or thread.get("thread_id") != thread_id or actual.get("graph_id") != "pm_agent"
            or actual.get("loopminder_project_id") != project_id
            or actual.get("loopminder_purpose") != "pm_workbench"):
        raise ValueError("恢复时官方会话身份不匹配，未接管该会话。")
    return thread_id


async def conversations(request):
    """List or create conversations belonging to a project."""
    if not local(request):
        return forbidden()
    try:
        data = await request.json() if request.method == "POST" else request.query_params
        project_id = data.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("请选择项目")
        store = await get_store()
        if await ProjectStore(store).load(project_id) is None:
            return JSONResponse({"error": "项目不存在"}, status_code=404)
        if request.method == "GET":
            return JSONResponse({"conversations": await list_conversations(store, project_id)})
        return JSONResponse(await create_conversation(store, get_client(url=None), project_id, data.get("title")))
    except (ValueError, TypeError, AttributeError) as exc:
        return JSONResponse({"error": str(exc) or "会话请求无效"}, status_code=400)
    except (httpx.HTTPError, TimeoutError):
        return JSONResponse({"error": "无法创建会话，请检查官方服务。"}, status_code=503)
