"""PM-specific projection of project-domain facts owned by issue #36."""

from dataclasses import dataclass

from src.middlewares.context_projection import ContextBlock, ContextProjection
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


def pm_context_projection(snapshot: ProjectSnapshot) -> ContextProjection:
    """Adapt current PM facts to #33's reviewed model-call projection surface."""
    pm_context = project_pm_context(snapshot)
    scope = snapshot.project_id
    blocks = (
        ContextBlock(
            block_id="goal",
            kind="project_goal",
            scope=scope,
            content=pm_context.goal,
            source="#36:ProjectSnapshot",
            priority=100,
            accepted=True,
        ),
        ContextBlock(
            block_id="constraints",
            kind="project_constraints",
            scope=scope,
            content="\n".join(pm_context.constraints),
            source="#36:ProjectSnapshot",
            priority=90,
            accepted=True,
        ),
        ContextBlock(
            block_id="active_tasks",
            kind="project_tasks",
            scope=scope,
            content="\n".join(
                f"{task.task_id}: {task.title} [{task.status}]"
                for task in pm_context.active_tasks
            ),
            source="#36:ProjectSnapshot",
            priority=80,
            accepted=True,
        ),
        ContextBlock(
            block_id="blockers",
            kind="project_blockers",
            scope=scope,
            content="\n".join(
                f"{task.task_id}: {task.title} [{task.status}]"
                for task in pm_context.blockers
            ),
            source="#36:ProjectSnapshot",
            priority=95,
            accepted=True,
        ),
    )
    return ContextProjection(scope=scope, blocks=blocks)
