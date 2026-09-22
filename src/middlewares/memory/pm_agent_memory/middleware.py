"""PM-specific project orientation and historical recall.

The middleware deliberately stays small:
- current project truth comes from #36 ProjectStore;
- historical PM memory lives in the shared LangGraph BaseStore;
- the PM only receives two tools: project_context and memory_search.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool

from src.middlewares.memory.revision_policy import MemoryRevisionState
from src.runtime.pm.context import project_pm_context
from src.runtime.project.persistence import ProjectStore

from .provider import pm_memory_store_provider

_PM_MEMORY_NAMESPACE = ("deep_loopminder", "pm_memory")
_SEARCH_SCAN_LIMIT = 200


def _project_id(runtime: ToolRuntime) -> str:
    project_id = runtime.config.get("configurable", {}).get("project_id")
    if not project_id:
        raise ValueError("project_id is required in config.configurable")
    return str(project_id)


def _memory_key(project_id: str, memory_ref: str) -> str:
    return f"{project_id}:{memory_ref}"


def _render_project_context(snapshot: Any) -> str:
    context = project_pm_context(snapshot)
    lines = [
        f"Project: {context.project_id}",
        f"Goal: {context.goal}",
    ]
    if context.constraints:
        lines.append("Constraints:")
        lines.extend(f"- {item}" for item in context.constraints)

    if context.active_tasks:
        lines.append("Active tasks:")
        lines.extend(
            f"- {task.task_id}: {task.title} [{task.status}]"
            for task in context.active_tasks
        )
    else:
        lines.append("Active tasks: none")

    if context.blockers:
        lines.append("Blockers:")
        lines.extend(
            f"- {task.task_id}: {task.title} [{task.status}]"
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


def _search_score(query: str, value: dict[str, Any]) -> int:
    query = query.casefold().strip()
    text = " ".join(
        [
            str(value.get("memory_ref", "")),
            str(value.get("kind", "")),
            str(value.get("content", "")),
            " ".join(str(ref) for ref in value.get("source_refs", ())),
        ]
    ).casefold()

    score = 4 if query in text else 0
    for term in query.split():
        score += text.count(term)
    return score


async def _search_project_memory(
    project_id: str,
    query: str,
    *,
    include_history: bool,
    limit: int,
) -> list[tuple[int, Any]]:
    store = await pm_memory_store_provider.get_store()
    filters: dict[str, Any] = {"project_id": project_id}
    if not include_history:
        filters["state"] = MemoryRevisionState.ACTIVE.value

    items = await store.asearch(
        _PM_MEMORY_NAMESPACE,
        filter=filters,
        limit=_SEARCH_SCAN_LIMIT,
    )
    ranked = [(_search_score(query, item.value), item) for item in items]
    ranked = [pair for pair in ranked if pair[0] > 0]
    ranked.sort(key=lambda pair: (-pair[0], str(pair[1].key)))
    return ranked[:limit]


@tool
async def memory_search(
    query: str,
    runtime: ToolRuntime,
    include_history: bool = False,
    limit: int = 5,
) -> str:
    """Search durable project history without treating it as current truth."""
    if not query.strip():
        raise ValueError("query must be non-empty")
    if limit < 1 or limit > 20:
        raise ValueError("limit must be between 1 and 20")

    project_id = _project_id(runtime)
    matches = await _search_project_memory(
        project_id,
        query,
        include_history=include_history,
        limit=limit,
    )
    if not matches:
        return "No relevant project memory found."

    sections: list[str] = []
    for _, item in matches:
        value = item.value
        memory_state = str(value.get("state", "unknown"))
        recall_status = (
            "superseded"
            if memory_state == MemoryRevisionState.SUPERSEDED.value
            else "historical"
        )
        sources = ", ".join(str(ref) for ref in value.get("source_refs", ())) or "none"
        sections.append(
            "\n".join(
                [
                    (
                        f"[{value.get('kind', 'memory')}:{value.get('memory_ref', item.key)} "
                        f"status={recall_status} memory_state={memory_state} "
                        f"version={value.get('version', 'unknown')}]"
                    ),
                    str(value.get("content", "")),
                    f"sources: {sources}",
                ]
            )
        )
    return "\n\n".join(sections)


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
    """Persist one durable project-memory revision.

    This is an internal runtime/consolidation entry point, not a PM-facing tool.
    """
    if not project_id or not memory_ref or not content.strip():
        raise ValueError("project_id, memory_ref, and content are required")

    store = await pm_memory_store_provider.get_store()

    if supersedes is not None:
        old_key = _memory_key(project_id, supersedes)
        previous = await store.aget(_PM_MEMORY_NAMESPACE, old_key)
        if previous is None:
            raise ValueError(f"superseded memory not found: {supersedes}")
        old_value = dict(previous.value)
        old_value["state"] = MemoryRevisionState.SUPERSEDED.value
        old_value["superseded_by"] = memory_ref
        await store.aput(_PM_MEMORY_NAMESPACE, old_key, old_value)

    value = {
        "project_id": project_id,
        "memory_ref": memory_ref,
        "kind": kind,
        "content": content,
        "source_refs": list(source_refs),
        "version": version,
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "state": MemoryRevisionState.ACTIVE.value,
        "superseded_by": None,
    }
    await store.aput(
        _PM_MEMORY_NAMESPACE,
        _memory_key(project_id, memory_ref),
        value,
        index=["content"],
    )


class PMMemoryMiddleware(AgentMiddleware):
    """Expose bounded PM orientation and historical recall tools."""

    tools: Sequence[BaseTool] = (project_context, memory_search)
