"""Cross-thread Project persistence on LangGraph's official ``BaseStore``.

Project business facts are application data, not LangGraph execution state. This
adapter deliberately delegates storage, durability, and cross-thread visibility
to the official Store interface instead of creating a second persistence runtime.
"""

from collections.abc import Mapping
from typing import Any

from langgraph.store.base import BaseStore

from src.runtime.project.models import ProjectSnapshot, Task, TaskStatus

_PROJECT_NAMESPACE = ("deep_loopminder", "projects")
_SCHEMA_VERSION = 1


class ProjectStore:
    """Persist and query Project snapshots through an injected LangGraph store."""

    def __init__(self, store: BaseStore) -> None:
        """Bind the adapter to the runtime's official LangGraph store."""
        self._store = store

    async def save(self, snapshot: ProjectSnapshot) -> None:
        """Persist one immutable business snapshot by project identity."""
        await self._store.aput(
            _PROJECT_NAMESPACE,
            snapshot.project_id,
            _snapshot_to_value(snapshot),
            index=False,
        )

    async def load(self, project_id: str) -> ProjectSnapshot | None:
        """Load one project independently of the thread performing the read."""
        if not project_id.strip():
            raise ValueError("project_id must be non-empty")
        item = await self._store.aget(_PROJECT_NAMESPACE, project_id)
        if item is None:
            return None
        return _snapshot_from_value(item.value)

    async def list(self, *, limit: int = 100, offset: int = 0) -> tuple[ProjectSnapshot, ...]:
        """List persisted projects using the official Store prefix query."""
        if limit < 1:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        items = await self._store.asearch(
            _PROJECT_NAMESPACE,
            limit=limit,
            offset=offset,
        )
        return tuple(_snapshot_from_value(item.value) for item in items)


def _snapshot_to_value(snapshot: ProjectSnapshot) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "project_id": snapshot.project_id,
        "goal": snapshot.goal,
        "constraints": list(snapshot.constraints),
        "tasks": [
            {
                "task_id": task.task_id,
                "title": task.title,
                "status": task.status.value,
                "owner_role": task.owner_role,
                "required_capabilities": sorted(task.required_capabilities),
                "dependencies": sorted(task.dependencies),
                "acceptance_criteria": list(task.acceptance_criteria),
                "priority": task.priority,
                "artifact_refs": list(task.artifact_refs),
                "execution_refs": list(task.execution_refs),
            }
            for task in snapshot.tasks
        ],
    }


def _snapshot_from_value(value: Mapping[str, Any]) -> ProjectSnapshot:
    if value.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("unsupported project snapshot schema_version")
    raw_tasks = value.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ValueError("project snapshot tasks must be a list")
    return ProjectSnapshot(
        project_id=str(value["project_id"]),
        goal=str(value["goal"]),
        constraints=tuple(str(item) for item in value.get("constraints", ())),
        tasks=tuple(_task_from_value(item) for item in raw_tasks),
    )


def _task_from_value(value: Mapping[str, Any]) -> Task:
    return Task(
        task_id=str(value["task_id"]),
        title=str(value["title"]),
        status=TaskStatus(str(value["status"])),
        owner_role=(None if value.get("owner_role") is None else str(value["owner_role"])),
        required_capabilities=frozenset(
            str(item) for item in value.get("required_capabilities", ())
        ),
        dependencies=frozenset(str(item) for item in value.get("dependencies", ())),
        acceptance_criteria=tuple(
            str(item) for item in value.get("acceptance_criteria", ())
        ),
        priority=int(value.get("priority", 0)),
        artifact_refs=tuple(str(item) for item in value.get("artifact_refs", ())),
        execution_refs=tuple(str(item) for item in value.get("execution_refs", ())),
    )
