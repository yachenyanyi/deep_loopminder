"""Resolve and preserve one official PM thread per project."""

import asyncio
import uuid

import httpx

from src.middlewares.project_state.scope import project_write_lock

from .context import THREADS as _THREADS
from .context import WRITE_LOCK as _write_lock

_PROJECT_THREAD_NAMESPACE = uuid.UUID("55483b1a-700f-41b2-9f18-c75a557e224c")

async def project_thread(store, client, project_id, *, create_if_missing=True):
    """Use one official PM thread per project in this local workbench."""
    async with _write_lock, project_write_lock(project_id):
        item = await store.aget(_THREADS, project_id)
        if item:
            thread_id = item.value.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                raise ValueError("项目的 PM 会话记录不完整；请核对恢复，未创建新会话。")
            offset = 0
            while True:
                bindings = await store.asearch(_THREADS, limit=100, offset=offset)
                if any(binding.key != project_id and binding.value.get("thread_id") == thread_id
                       for binding in bindings):
                    raise ValueError("多个项目指向同一 PM 会话；请人工核对，暂不能读取或续接。")
                if len(bindings) < 100:
                    break
                offset += len(bindings)
            try:
                async with asyncio.timeout(15):
                    thread = await client.threads.get(thread_id)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    raise ValueError(
                        "已关联的 PM 会话在官方服务中不存在；原服务和当前服务可能使用不同的线程注册表。"
                        "请先在原服务核对会话及未完成运行；数据库中存在检查点并不代表线程可以恢复。"
                        "未修改项目绑定，也未创建新会话。"
                    ) from exc
                raise ValueError("无法核对已关联的 PM 会话，请检查官方服务。") from exc
            except (httpx.HTTPError, TimeoutError) as exc:
                raise ValueError("无法核对已关联的 PM 会话，请检查官方服务。") from exc
            metadata = thread.get("metadata") or {}
            if (thread.get("thread_id") != thread_id
                    or not isinstance(metadata, dict)
                    or metadata.get("loopminder_project_id") not in (None, project_id)
                    or metadata.get("loopminder_purpose") not in (None, "pm_workbench")
                    or (item.value.get("identity_version") == 1
                        and (metadata.get("loopminder_project_id") != project_id
                             or metadata.get("loopminder_purpose") != "pm_workbench"))):
                raise ValueError("已关联的 PM 会话身份不匹配，请人工核对。")
            return thread_id
        if not create_if_missing:
            raise ValueError("项目的 PM 会话记录已变化，请重新核对。")
        # A lost create response must not leave an untraceable second PM
        # thread. The official API supports caller-supplied UUIDs and
        # if_exists=do_nothing; verify ownership even when it already exists.
        thread_id = str(uuid.uuid5(_PROJECT_THREAD_NAMESPACE, project_id))
        try:
            async with asyncio.timeout(15):
                thread = await client.threads.create(
                    thread_id=thread_id, if_exists="do_nothing",
                    metadata={"loopminder_project_id": project_id, "loopminder_purpose": "pm_workbench"},
                )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ValueError("无法确认 PM 会话创建结果；请检查官方服务后重试核对同一会话。") from exc
        metadata = thread.get("metadata") or {}
        if (thread.get("thread_id") != thread_id or metadata.get("loopminder_project_id") != project_id
                or metadata.get("loopminder_purpose") != "pm_workbench"):
            raise ValueError("现有官方会话身份不匹配，请人工核对；未创建新的项目绑定。")
        await store.aput(_THREADS, project_id, {"thread_id": thread["thread_id"], "identity_version": 1}, index=False)
        return thread["thread_id"]


