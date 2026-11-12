"""Project-scoped memory documents on the official StoreBackend."""

from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import PurePosixPath
import re
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
    if result.next_offset is not None:
        raise ValueError("记忆文件超过安全读取范围，无法核验完整内容")
    return result.file_data["content"]


async def latest_handoff(
    store: BaseStore, project_id: str, thread_id: str
) -> str | None:
    """Load only handoffs that the memory Agent finished and verified as readable."""
    offset = 0
    latest = None
    while True:
        page = await store.asearch(
            ("deep_loopminder", "pm_memory_sources", project_id),
            filter={"handoff_ready": True, "thread_id": thread_id},
            limit=100,
            offset=offset,
        )
        for item in page:
            if latest is None or item.value["created_at"] > latest["created_at"]:
                latest = item.value
        if len(page) < 100:
            return latest["destination"] if latest else None
        offset += len(page)


def document_summary(body: str) -> tuple[str, str]:
    """Extract the title and first prose paragraph from a Markdown document."""
    lines = body.splitlines()
    title = next(
        (line.lstrip("# ") for line in lines if line.startswith("#")), "Memory"
    )
    prose = [
        line.strip()
        for line in lines
        if line.strip() and not line.startswith(("#", "<!--"))
    ]
    return title, " ".join(prose)[:300]


async def memory_candidates(
    store: BaseStore,
    project_id: str,
    query: str,
    *,
    limit: int = 5,
    include_history: bool = False,
) -> list[dict]:
    """Use official Store search when indexed, otherwise the backend's literal grep."""
    namespace = ("deep_loopminder", "pm_memory_documents", project_id)
    if getattr(store, "index_config", None):
        items = await store.asearch(
            namespace,
            query=query,
            filter=None if include_history else {"state": "historical"},
            limit=limit,
        )
        return [item.value for item in items]
    result = await memory_files(store, project_id).agrep(
        query, path="/", glob="**/*.md"
    )
    if result.error:
        raise RuntimeError(result.error)
    candidates = []
    for path in dict.fromkeys(match["path"] for match in (result.matches or [])):
        meta = await store.aget(namespace, path)
        metadata = meta.value if meta else {}
        if not include_history and metadata.get("state") == "superseded":
            continue
        title, summary = document_summary(
            await read_document(store, project_id, "/memories" + path)
        )
        candidates.append(
            {
                "path": "/memories" + path,
                "title": title,
                "summary": summary,
                "state": metadata.get("state", "historical"),
                "updated_at": metadata.get("updated_at"),
                "version": metadata.get("version"),
                "source_refs": metadata.get("source_refs", []),
            }
        )
        if len(candidates) == limit:
            break
    return candidates


async def memory_context(
    store: BaseStore,
    project_id: str,
    thread_id: str,
    *,
    recent_days: int = 3,
    today: date | None = None,
    timezone: str = "Asia/Shanghai",
    max_chars: int = 8_000,
    query: str = "",
) -> str:
    """Render independently budgeted continuity, discovery and source sections."""
    today = today or datetime.now(ZoneInfo(timezone)).date()
    paths = await document_paths(store, project_id)
    sections = []
    handoff = await latest_handoff(store, project_id, thread_id)
    if handoff:
        body = await read_document(store, project_id, handoff)
        sections.append((f"Latest handoff: {handoff}\n{body}", 0.35))
    topics = []
    for path in [path for path in paths if path.startswith("/topics/")][:30]:
        meta = await store.aget(
            ("deep_loopminder", "pm_memory_documents", project_id), path
        )
        if meta and meta.value.get("state") == "superseded":
            continue
        body = await read_document(store, project_id, "/memories" + path)
        title, summary = document_summary(body)
        topics.append(f"/memories{path}: {title}\n{summary[:250]}")
    if topics:
        sections.append(("Memory Map:\n" + "\n".join(topics), 0.2))
    cutoff = today - timedelta(days=max(recent_days - 1, 0))
    daily = []
    if recent_days:
        for path in sorted(
            (path for path in paths if path.startswith("/daily/")), reverse=True
        ):
            try:
                stem = PurePosixPath(path).stem
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:-[0-9a-f]{16})?", stem):
                    continue
                day = date.fromisoformat(stem[:10])
            except ValueError:
                continue
            if cutoff <= day <= today:
                body = await read_document(store, project_id, "/memories" + path)
                daily.append(f"Daily /memories{path}: {body[:800]}")
    if daily:
        sections.append(("Recent daily summaries:\n" + "\n".join(daily), 0.2))
    if query:
        candidates = await memory_candidates(store, project_id, query)
        if candidates:
            rows = [
                f"{row['path']}: {row['title']}\n{row['summary']}\nSources: {row['source_refs']} Updated: {row['updated_at']}"
                for row in candidates
            ]
            sections.append(
                (
                    "Candidates for the current historical question (open selectively):\n"
                    + "\n".join(rows),
                    0.15,
                )
            )
    bookmarks = []
    offset = 0
    while True:
        page = await store.asearch(
            ("deep_loopminder", "pm_memory_sources", project_id),
            limit=100,
            offset=offset,
        )
        bookmarks.extend(item.value for item in page)
        if len(page) < 100:
            break
        offset += len(page)
    bookmarks.sort(key=lambda job: job["created_at"], reverse=True)
    if bookmarks:
        rows = [
            f"{job['id']} {job['created_at']} thread={job['thread_id']} {job['purpose']}: {job.get('observation', '')[:150]}"
            for job in bookmarks[:5]
        ]
        sections.append(
            ("Recent source bookmarks (pm_read_bookmark):\n" + "\n".join(rows), 0.1)
        )
    lines = ["Historical PM memory; current truth belongs to project_context()."]
    budget = max(0, max_chars - len(lines[0]) - 300)
    for content, fraction in sections:
        size = int(budget * fraction)
        lines.append(
            content[:size]
            + (
                "\n[Open the referenced document for more.]"
                if len(content) > size
                else ""
            )
        )
    return "\n\n".join(lines)[:max_chars]
