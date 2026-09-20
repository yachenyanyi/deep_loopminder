"""PM-specific projection of project-domain facts owned by issue #36."""

from dataclasses import dataclass

from ..project import ProjectSnapshot, Task, TaskStatus


_TERMINAL_TASK_STATUSES = frozenset({TaskStatus.DONE})
_BLOCKED_TASK_STATUSES = frozenset(
    {TaskStatus.FAILED, TaskStatus.REWORK, TaskStatus.WAITING_HUMAN}
)


@dataclass(frozen=True, slots=True)
class PMContextProjection:
    """Bounded PM view derived from the current project snapshot."""

    project_id: str
    goal: str
    constraints: tuple[str, ...]
    active_tasks: tuple[Task, ...]
    blockers: tuple[Task, ...]


def project_pm_context(snapshot: ProjectSnapshot) -> PMContextProjection:
    """Select PM-level business facts without copying runtime or provider state."""
    active_tasks = tuple(
        task for task in snapshot.tasks if task.status not in _TERMINAL_TASK_STATUSES
    )
    blockers = tuple(
        task for task in active_tasks if task.status in _BLOCKED_TASK_STATUSES
    )
    return PMContextProjection(
        project_id=snapshot.project_id,
        goal=snapshot.goal,
        constraints=snapshot.constraints,
        active_tasks=active_tasks,
        blockers=blockers,
    )
