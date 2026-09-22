"""PM-specific project orientation and historical recall.

This middleware stays deliberately small:
- current project truth comes from #36 ProjectStore;
- historical memory lives in the existing LangGraph BaseStore;
- PM sees only project_context() and memory_search().
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import StructuredTool
from langgraph.config import get_config
from langgraph.store.base import Item

from src.middlewares.memory.revision_policy import MemoryRevisionState
from src.runtime.pm.context import project_pm_context
from src.runtime.project import get_project_store

from .provider import get_pm_memory_store

_PM_MEMORY_NAMESPACE = ("deep_loopminder", "pm_memory")
_KEYWORD_SCAN_LIMIT = 100
_MAX_RESULT_CHARS = 1_200


class PMAgentMemoryMiddleware(AgentMiddleware):
    """Give the PM a current project map and bounded historical recall."""

    @property
    def tools(self) -> list[StructuredTool]:
        """Expose only the two PM-facing primitives from Issue #40."""
        return [
            StructuredTool.from_function(
                coroutine=self.project_context,
                name="project_context",
                description=(
                    "Read the authoritative current project orientation: goal, "
                    "constraints, active tasks and blockers."
                ),
            ),
            StructuredTool.from_function(
                coroutine=self.memory_search,
                name="memory_search",
                description=(
                    "Search historical project memory when current project state "
                    "is not enough to understand past decisions or context."
                ),
            ),
        ]

    async def project_context(self, project_id: str | None = None) -> str:
        """Return a compact current project map from authoritative Project State."""
        resolved_project_id = _project_id(project_id)
        snapshot = await get_project_store().load(resolved_project_id)
        if snapshot is None:
            return f"Project {resolved_project_id!r} was not found."

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

    async def memory_search(
        self,
        query: str,
        project_id: str | None = None,
        limit: int = 8,
    ) -> str:
        """Search active historical memory for one project.

        Memory results are references for reasoning, not authoritative current
        project state.
        """
        if not query.strip():
            raise ValueError("query must be non-empty")
        if limit < 1:
            raise ValueError("limit must be positive")

        resolved_project_id = _project_id(project_id)
        provider = await get_pm_memory_store()
        namespace = _memory_namespace(resolved_project_id)

        if getattr(provider.store, "index_config", None):
            items = await provider.store.asearch(
                namespace,
                filter={"state": MemoryRevisionState.ACTIVE.value},
                query=query,
                limit=limit,
            )
        else:
            candidates = await provider.store.asearch(
                namespace,
                filter={"state": MemoryRevisionState.ACTIVE.value},
                limit=_KEYWORD_SCAN_LIMIT,
            )
            items = _keyword_matches(candidates, query, limit)

        if not items:
            return "No matching project memory."

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
            source = value.get("source_ref")
            source_text = f" source={source}" if source else ""
            lines.append(
                f"- [{value.get('kind', 'memory')}] {item.key}"
                f" state={value.get('state')}{source_text}\n  {content}"
            )

        return "\n".join(lines)

    async def save_memory(
        self,
        project_id: str,
        memory_ref: str,
        content: str,
        *,
        kind: str = "note",
        source_ref: str | None = None,
        supersedes: str | None = None,
    ) -> None:
        """Persist one admitted PM memory revision.

        This is an internal runtime/consolidation entrypoint, not a PM-facing tool.
        """
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
            previous_value["state"] = MemoryRevisionState.SUPERSEDED.value
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
                "kind": kind,
                "content": content,
                "source_ref": source_ref,
                "state": MemoryRevisionState.ACTIVE.value,
                "superseded_by": None,
            },
            index=["content"],
        )


def _project_id(project_id: str | None) -> str:
    if project_id and project_id.strip():
        return project_id.strip()

    configurable = get_config().get("configurable", {})
    configured_project_id = configurable.get("project_id")
    if not configured_project_id:
        raise ValueError("project_id is required")
    return str(configured_project_id)


def _memory_namespace(project_id: str) -> tuple[str, ...]:
    return (*_PM_MEMORY_NAMESPACE, project_id)


def _keyword_matches(items: list[Item], query: str, limit: int) -> list[Item]:
    query_text = query.casefold().strip()
    terms = [term for term in query_text.split() if term]
    ranked: list[tuple[int, Item]] = []

    for item in items:
        value = item.value
        haystack = " ".join(
            str(value.get(field, ""))
            for field in ("kind", "content", "source_ref")
        ).casefold()

        score = 10 if query_text in haystack else 0
        score += sum(1 for term in terms if term in haystack)
        if score:
            ranked.append((score, item))

    ranked.sort(key=lambda pair: (pair[0], pair[1].updated_at), reverse=True)
    return [item for _, item in ranked[:limit]]
