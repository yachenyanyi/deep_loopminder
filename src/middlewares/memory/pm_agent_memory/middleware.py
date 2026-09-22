"""PM-specific project orientation and historical recall.

Current truth stays in #36 ProjectStore. Historical PM memory uses the existing
LangGraph BaseStore provider. The PM only receives project_context() and
memory_search().
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool
from langgraph.store.base import Item

from src.runtime.pm.context import project_pm_context
from src.runtime.project.persistence import ProjectStore

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
    provider = await get_pm_memory_store()
    namespace = _memory_namespace(project_id)
    state_filter = (
        None
        if include_history
        else {"state": _ACTIVE_STATE}
    )

    if getattr(provider.store, "index_config", None):
        items = await provider.store.asearch(
            namespace,
            filter=state_filter,
            query=query,
            limit=limit,
        )
    else:
        candidates = await provider.store.asearch(
            namespace,
            filter=state_filter,
            limit=_SEARCH_SCAN_LIMIT,
        )
        items = _keyword_matches(candidates, query, limit)

    if not items:
        return "No relevant project memory found."

    lines = [
        "Historical project memory. Validate mutable facts against project_context()."
    ]
    if provider.durability != "durable":
        lines.append(
            "Storage warning: PM memory is currently ephemeral and may be lost "
            "when the process exits."
        )

    for item in items:
        value = item.value
        content = str(value.get("content", "")).strip()
        if len(content) > _MAX_RESULT_CHARS:
            content = content[:_MAX_RESULT_CHARS].rstrip() + "…"

        memory_state = str(value.get("state", "unknown"))
        status = (
            "superseded"
            if memory_state == _SUPERSEDED_STATE
            else "historical"
        )
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


class PMAgentMemoryMiddleware(AgentMiddleware):
    """Expose the two bounded PM memory tools from Issue #40."""

    tools: Sequence[BaseTool] = (project_context, memory_search)
