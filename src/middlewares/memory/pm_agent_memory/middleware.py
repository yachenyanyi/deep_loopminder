"""PM-specific project orientation and historical recall.

Current truth stays in #36 ProjectStore. Historical PM memory and frozen source
snapshots use the runtime's shared LangGraph BaseStore. Background writes are
enabled only when a PMMemoryJobs consumer is explicitly supplied.
"""

from __future__ import annotations

import hashlib
import json
import operator
from collections.abc import Sequence
from typing import Annotated, NotRequired

from deepagents.backends import CompositeBackend, StoreBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.tools import ToolRuntime
from langchain_core.messages import SystemMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import BaseTool, tool
from langgraph.config import get_config
from langgraph.store.base import BaseStore, Item
from langgraph.store.memory import InMemoryStore

from src.middlewares.execution.security_policy import scoped_filesystem_permissions
from src.runtime.pm.context import project_pm_context
from src.runtime.project.persistence import ProjectStore

from .files import document_paths, memory_context, read_document
from .jobs import MEMORY_EVENTS, PMMemoryJobs
from .provider import get_pm_memory_store

_PM_MEMORY_NAMESPACE = ("deep_loopminder", "pm_memory")
_SEARCH_SCAN_LIMIT = 100
_MAX_RESULT_CHARS = 1_200
_ACTIVE_STATE = "active"
_SUPERSEDED_STATE = "superseded"


def _project_id(runtime: ToolRuntime) -> str:
    project_id = runtime.config.get("configurable", {}).get("project_id")
    if not project_id:
        raise ValueError("project_id is required in config.configurable")
    return str(project_id)


def _memory_namespace(project_id: str) -> tuple[str, ...]:
    return (*_PM_MEMORY_NAMESPACE, project_id)


def project_memory_filesystem_middleware(
    *,
    project_id: str,
    store: BaseStore,
) -> tuple[FilesystemMiddleware, list]:
    """Assemble the official project-scoped filesystem surface for memory work.

    Deep Agents exposes filesystem permissions on the public ``create_deep_agent``
    assembly boundary, not as a public ``FilesystemMiddleware`` constructor
    argument. Return both pieces so the eventual memory-agent assembly can pass
    ``backend=filesystem.backend`` and ``permissions=permissions`` directly to
    ``create_deep_agent`` without using the private ``_permissions`` parameter.
    """
    if not project_id.strip():
        raise ValueError("project_id must be non-empty")

    backend = CompositeBackend(
        default=StoreBackend(
            namespace=lambda _runtime: (
                "deep_loopminder",
                "pm_memory_scratch",
                project_id,
            ),
            store=store,
        ),
        routes={
            "/memories/": StoreBackend(
                namespace=lambda _runtime: (
                    "deep_loopminder",
                    "pm_memory_files",
                    project_id,
                ),
                store=store,
            )
        },
    )
    return (
        FilesystemMiddleware(backend=backend),
        scoped_filesystem_permissions("/memories"),
    )


def _render_project_context(snapshot) -> str:
    context = project_pm_context(snapshot)
    lines = [
        f"Project: {context.project_id}",
        f"Goal: {context.goal}",
    ]

    if context.constraints:
        lines.append("Constraints:")
        lines.extend(f"- {constraint}" for constraint in context.constraints)

    if context.active_tasks:
        lines.append("Active tasks:")
        for task in context.active_tasks:
            owner = f" owner={task.owner_role}" if task.owner_role else ""
            dependencies = (
                f" deps={','.join(sorted(task.dependencies))}"
                if task.dependencies
                else ""
            )
            lines.append(
                f"- {task.task_id}: {task.title} [{task.status.value}]"
                f"{owner}{dependencies}"
            )
    else:
        lines.append("Active tasks: none")

    if context.blockers:
        lines.append("Blockers:")
        lines.extend(
            f"- {task.task_id}: {task.title} [{task.status.value}]"
            for task in context.blockers
        )
    else:
        lines.append("Blockers: none")

    return "\n".join(lines)


@tool
async def project_context(runtime: ToolRuntime) -> str:
    """Read the authoritative current project map for PM orientation."""
    project_id = _project_id(runtime)
    if runtime.store is None:
        raise RuntimeError("project_context requires the LangGraph runtime Store")

    snapshot = await ProjectStore(runtime.store).load(project_id)
    if snapshot is None:
        return f"Project state not found: {project_id}"
    return _render_project_context(snapshot)


def _keyword_matches(items: list[Item], query: str, limit: int) -> list[Item]:
    query_text = query.casefold().strip()
    terms = [term for term in query_text.split() if term]
    ranked: list[tuple[int, Item]] = []

    for item in items:
        value = item.value
        text = " ".join(
            [
                item.key,
                str(value.get("kind", "")),
                str(value.get("content", "")),
                " ".join(str(ref) for ref in value.get("source_refs", ())),
            ]
        ).casefold()

        score = 10 if query_text in text else 0
        score += sum(1 for term in terms if term in text)
        if score:
            ranked.append((score, item))

    ranked.sort(key=lambda pair: (pair[0], pair[1].updated_at), reverse=True)
    return [item for _, item in ranked[:limit]]


@tool
async def memory_search(
    query: str,
    runtime: ToolRuntime,
    include_history: bool = False,
    limit: int = 5,
) -> str:
    """Search project history without treating recalled memory as current truth."""
    if not query.strip():
        raise ValueError("query must be non-empty")
    if limit < 1 or limit > 20:
        raise ValueError("limit must be between 1 and 20")

    project_id = _project_id(runtime)
    if runtime.store is not None:
        store = runtime.store
        ephemeral = isinstance(store, InMemoryStore)
    else:
        provider = await get_pm_memory_store()
        store = provider.store
        ephemeral = provider.durability != "durable"
    namespace = _memory_namespace(project_id)
    state_filter = None if include_history else {"state": _ACTIVE_STATE}

    file_matches = []
    for path in await document_paths(store, project_id):
        if not path.startswith(("/topics/", "/daily/", "/handoffs/")):
            continue
        body = await read_document(store, project_id, "/memories" + path)
        if all(term in (path + " " + body).casefold() for term in query.casefold().split()):
            file_matches.append(f"[file:/memories{path}]\n{body[:_MAX_RESULT_CHARS]}\nUse pm_read_memory for the full document.")
        if len(file_matches) == limit:
            break

    if getattr(store, "index_config", None):
        items = await store.asearch(
            namespace,
            filter=state_filter,
            query=query,
            limit=limit,
        )
    else:
        candidates = await store.asearch(
            namespace,
            filter=state_filter,
            limit=_SEARCH_SCAN_LIMIT,
        )
        items = _keyword_matches(candidates, query, limit)

    if not items and not file_matches:
        return "No relevant project memory found."

    lines = [
        "Historical project memory. Validate mutable facts against project_context()."
    ]
    if ephemeral:
        lines.append(
            "Storage warning: PM memory is currently ephemeral and may be lost "
            "when the process exits."
        )

    lines.extend(file_matches)
    for item in items[:limit - len(file_matches)]:
        value = item.value
        content = str(value.get("content", "")).strip()
        if len(content) > _MAX_RESULT_CHARS:
            content = content[:_MAX_RESULT_CHARS].rstrip() + "…"

        memory_state = str(value.get("state", "unknown"))
        status = "superseded" if memory_state == _SUPERSEDED_STATE else "historical"
        sources = ", ".join(str(ref) for ref in value.get("source_refs", ())) or "none"
        lines.append(
            "\n".join(
                [
                    (
                        f"[{value.get('kind', 'memory')}:{item.key} "
                        f"status={status} memory_state={memory_state} "
                        f"version={value.get('version', 'unknown')}]"
                    ),
                    content,
                    f"sources: {sources}",
                ]
            )
        )

    return "\n\n".join(lines)


async def remember_project_memory(
    project_id: str,
    memory_ref: str,
    content: str,
    *,
    kind: str = "note",
    source_refs: Sequence[str] = (),
    version: str = "1",
    supersedes: str | None = None,
) -> None:
    """Persist one admitted PM memory revision for runtime/consolidation code."""
    if not project_id.strip() or not memory_ref.strip() or not content.strip():
        raise ValueError("project_id, memory_ref and content must be non-empty")

    provider = await get_pm_memory_store()
    store = provider.store
    namespace = _memory_namespace(project_id)

    if supersedes:
        previous = await store.aget(namespace, supersedes)
        if previous is None:
            raise ValueError(f"memory to supersede was not found: {supersedes}")
        previous_value = dict(previous.value)
        previous_value["state"] = _SUPERSEDED_STATE
        previous_value["superseded_by"] = memory_ref
        await store.aput(
            namespace,
            supersedes,
            previous_value,
            index=["content"],
        )

    await store.aput(
        namespace,
        memory_ref,
        {
            "project_id": project_id,
            "kind": kind,
            "content": content,
            "source_refs": list(source_refs),
            "version": version,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "state": _ACTIVE_STATE,
            "superseded_by": None,
        },
        index=["content"],
    )


class PMMemoryState(AgentState):
    """Checkpoint trigger progress rather than keeping process-local watermarks."""

    pm_memory_events: NotRequired[Annotated[list[dict], operator.add]]
    pm_memory_event_cursor: NotRequired[int]
    pm_memory_token_watermark: NotRequired[int]


class PMAgentMemoryMiddleware(AgentMiddleware):
    """Orient PM calls and optionally schedule snapshot-backed memory work."""

    tools: Sequence[BaseTool] = (project_context, memory_search)
    state_schema = PMMemoryState

    def __init__(self, jobs: PMMemoryJobs | None = None, *, recent_daily_days: int = 3, context_token_threshold: int | None = None):
        """Keep writes opt-in until a memory model/consumer is configured."""
        if recent_daily_days < 0 or (context_token_threshold is not None and context_token_threshold < 1):
            raise ValueError("invalid memory window or token threshold")
        self.jobs = jobs
        self.recent_daily_days = recent_daily_days
        self.context_token_threshold = context_token_threshold
        self.tools = list(type(self).tools)
        if jobs is None:
            return

        @tool
        async def submit_pm_memory(purpose: str, runtime: ToolRuntime, observation: str = "", focus: str = "") -> str:
            """Ask the background memory agent to preserve selected observations."""
            project_id, thread_id = self._scope(runtime.config, runtime.store)
            job = await jobs.submit(project_id, thread_id, runtime.state["messages"], purpose=purpose, observation=observation, focus=focus)
            return json.dumps({"job_id": job["id"], "status": job["status"], "bookmark": job["bookmark"]})

        @tool
        async def pm_memory_status(job_id: str, runtime: ToolRuntime) -> str:
            """Read background progress without automatically retrying failures."""
            project_id, _ = self._scope(runtime.config, runtime.store)
            job = await jobs.status(project_id, job_id)
            return json.dumps({key: job.get(key) for key in ("id", "status", "destination", "bookmark", "error")})

        @tool
        async def pm_read_memory(path: str, runtime: ToolRuntime, offset: int = 0, limit: int = 100) -> str:
            """Read a chosen memory document by lines; offset starts at zero."""
            if offset < 0 or not 1 <= limit <= 200:
                raise ValueError("offset >= 0 and limit between 1 and 200 are required")
            project_id, _ = self._scope(runtime.config, runtime.store)
            body = await read_document(jobs.store, project_id, path)
            lines = body.splitlines()
            text = "\n".join(lines[offset:offset + limit])
            return json.dumps({"path": path, "offset": offset, "total_lines": len(lines), "content": text}, ensure_ascii=False)

        @tool
        async def pm_read_bookmark(job_id: str, runtime: ToolRuntime, offset: int = 0, limit: int = 6_000) -> str:
            """Read a frozen bookmark source as paged JSON text, including failed jobs."""
            if offset < 0 or not 1 <= limit <= 20_000:
                raise ValueError("offset >= 0 and limit between 1 and 20000 are required")
            project_id, _ = self._scope(runtime.config, runtime.store)
            job = await jobs.status(project_id, job_id)
            source = json.dumps({"messages": job["snapshot"], "project_state": job.get("project_snapshot")}, ensure_ascii=False)
            return json.dumps({"job_id": job_id, "offset": offset, "total_chars": len(source), "content": source[offset:offset + limit]}, ensure_ascii=False)

        self.tools.extend([submit_pm_memory, pm_memory_status, pm_read_memory, pm_read_bookmark])

    def _scope(self, config: dict, store: BaseStore | None) -> tuple[str, str]:
        configurable = config.get("configurable", {})
        project_id, thread_id = configurable.get("project_id"), configurable.get("thread_id")
        if not project_id or not thread_id:
            raise ValueError("project_id and thread_id are required")
        if self.jobs and store is not None and store is not self.jobs.store:
            raise ValueError("PM runtime and memory jobs must use the same Store")
        return str(project_id), str(thread_id)

    async def awrap_model_call(self, request, handler):
        """Append live state and historical orientation without replacing Skills."""
        config = get_config()
        project_id, thread_id = self._scope(config, request.runtime.store)
        store = request.runtime.store or (self.jobs.store if self.jobs else None)
        if store is None:
            return await handler(request)
        if self.jobs:
            await self.jobs.resume_pending(project_id)
        snapshot = await ProjectStore(store).load(project_id)
        current = _render_project_context(snapshot) if snapshot else f"Project state not found: {project_id}"
        history = await memory_context(store, project_id, thread_id, recent_days=self.recent_daily_days, timezone=self.jobs.timezone.key if self.jobs else "Asia/Shanghai")
        content = f"Current project state:\n{current}\n\n{history}"
        if request.system_message:
            system = request.system_message.model_copy(update={"content": [*request.system_message.content_blocks, {"type": "text", "text": content}]})
        else:
            system = SystemMessage(content=content)
        return await handler(request.override(system_message=system))

    async def aafter_agent(self, state, runtime):
        """Observe appended PM events and context growth at the end of a turn."""
        if self.jobs is None:
            return None
        project_id, thread_id = self._scope(get_config(), runtime.store)
        messages = state["messages"]
        events = state.get("pm_memory_events", [])
        cursor = state.get("pm_memory_event_cursor", 0)
        for event in events[cursor:]:
            if event.get("kind") not in MEMORY_EVENTS or not event.get("id"):
                raise ValueError("PM memory events require a stable id and supported kind")
            event_key = hashlib.sha256(f"{thread_id}:{event['id']}".encode()).hexdigest()
            await self.jobs.submit(project_id, thread_id, messages, purpose=f"Review PM event: {event['kind']}", observation=json.dumps(event, ensure_ascii=False), job_id=event_key)
        tokens = count_tokens_approximately(messages)
        watermark = state.get("pm_memory_token_watermark", 0)
        threshold = self.context_token_threshold
        if tokens < watermark or (threshold and tokens < threshold):
            watermark = 0
        if threshold and tokens >= max(threshold, watermark + threshold):
            source = json.dumps([message.model_dump(mode="json") for message in messages], sort_keys=True)
            job_id = hashlib.sha256(f"{thread_id}:{source}".encode()).hexdigest()
            await self.jobs.submit(project_id, thread_id, messages, purpose="Review accumulated PM context", job_id=job_id)
            watermark = tokens
        return {"pm_memory_event_cursor": len(events), "pm_memory_token_watermark": watermark}
