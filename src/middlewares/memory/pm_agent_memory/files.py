"""Project-scoped memory documents on the official StoreBackend."""

from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

from deepagents.backends import StoreBackend
from langgraph.store.base import BaseStore


def file_namespace(project_id: str) -> tuple[str, ...]:
    """Keep document identity separate from project state and legacy records."""
    return ("deep_loopminder", "pm_memory_files", project_id)


def memory_files(store: BaseStore, project_id: str) -> StoreBackend:
    """Use the same backend as the background agent's /memories route."""
    return StoreBackend(
        store=store, namespace=lambda _runtime: file_namespace(project_id)
    )


def thread_key(thread_id: str) -> str:
    """Make a stable path component without embedding an arbitrary thread ID."""
    return sha256(thread_id.encode()).hexdigest()[:24]


async def document_paths(store: BaseStore, project_id: str) -> list[str]:
    """Page through documents using native async Store operations."""
    paths = []
    offset = 0
    while True:
        page = await store.asearch(file_namespace(project_id), limit=100, offset=offset)
        paths.extend(item.key for item in page)
        if len(page) < 100:
            return sorted(paths)
        offset += len(page)


async def read_document(store: BaseStore, project_id: str, path: str) -> str:
    """Read a virtual document; never resolve a host filesystem path."""
    if not path.startswith("/memories/") or ".." in PurePosixPath(path).parts:
        raise ValueError("path must stay inside /memories/")
    result = await memory_files(store, project_id).aread(
        path.removeprefix("/memories"), limit=100_000
    )
    if result.error:
        raise FileNotFoundError(result.error)
    return result.file_data["content"]


async def latest_handoff(
    store: BaseStore, project_id: str, thread_id: str
) -> str | None:
    """Only completed jobs may become a thread's automatic resume context."""
    offset = 0
    latest = None
    while True:
        page = await store.asearch(
            ("deep_loopminder", "pm_memory_jobs", project_id),
            filter={"status": "completed", "handoff": True, "thread_id": thread_id},
            limit=100,
            offset=offset,
        )
        for item in page:
            if latest is None or item.value["created_at"] > latest["created_at"]:
                latest = item.value
        if len(page) < 100:
            return latest["destination"] if latest else None
        offset += len(page)


async def memory_context(
    store: BaseStore,
    project_id: str,
    thread_id: str,
    *,
    recent_days: int = 3,
    today: date | None = None,
    timezone: str = "Asia/Shanghai",
    max_chars: int = 8_000,
) -> str:
    """Load this thread's latest handoff, a topic map and recent daily summaries."""
    today = today or datetime.now(ZoneInfo(timezone)).date()
    paths = await document_paths(store, project_id)
    handoff_path = await latest_handoff(store, project_id, thread_id)
    lines = ["Historical PM memory; current truth belongs to project_context()."]
    if handoff_path:
        path = handoff_path
        body = await read_document(store, project_id, path)
        lines.append(f"Latest handoff: {path}\n{body[:4_000]}")
    topics = [p for p in paths if p.startswith("/topics/")][:30]
    for path in topics:
        body = await read_document(store, project_id, "/memories" + path)
        lines.append(f"Topic /memories{path}: {body[:250]}")
    cutoff = today - timedelta(days=max(recent_days - 1, 0))
    if recent_days:
        for path in sorted((p for p in paths if p.startswith("/daily/")), reverse=True):
            try:
                day = date.fromisoformat(PurePosixPath(path).stem)
            except ValueError:
                continue
            if cutoff <= day <= today:
                body = await read_document(store, project_id, "/memories" + path)
                lines.append(f"Daily /memories{path}: {body[:800]}")
    text = "\n\n".join(lines)
    return (
        text
        if len(text) <= max_chars
        else text[:max_chars] + "\n[Use pm_read_memory for full documents.]"
    )
