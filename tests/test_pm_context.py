"""Tests for the bounded PM project-context projection."""

from src.runtime.pm import PMContextProjection, project_pm_context
from src.runtime.project import ProjectSnapshot, Task, TaskStatus


def _task(task_id: str, status: TaskStatus) -> Task:
    return Task(
        task_id=task_id,
        title=f"Task {task_id}",
        status=status,
        owner_role="developer",
        required_capabilities=frozenset({"python"}),
        dependencies=frozenset(),
        acceptance_criteria=("validated",),
        artifact_refs=("artifact://result",),
        execution_refs=("run://official-runtime",),
    )


def test_projection_keeps_current_pm_level_project_facts() -> None:
    snapshot = ProjectSnapshot(
        project_id="project-1",
        goal="Ship the project",
        constraints=("AI branch only",),
        tasks=(_task("active", TaskStatus.IN_PROGRESS),),
    )

    projection = project_pm_context(snapshot)

    assert projection == PMContextProjection(
        project_id="project-1",
        goal="Ship the project",
        constraints=("AI branch only",),
        active_tasks=snapshot.tasks,
        blockers=(),
    )


def test_projection_excludes_done_tasks_from_active_context() -> None:
    active = _task("active", TaskStatus.READY)
    done = _task("done", TaskStatus.DONE)
    snapshot = ProjectSnapshot(
        project_id="project-1",
        goal="Ship",
        constraints=(),
        tasks=(done, active),
    )

    projection = project_pm_context(snapshot)

    assert projection.active_tasks == (active,)
    assert done not in projection.blockers


def test_projection_surfaces_project_blocker_statuses_without_runtime_copy() -> None:
    failed = _task("failed", TaskStatus.FAILED)
    waiting = _task("waiting", TaskStatus.WAITING_HUMAN)
    snapshot = ProjectSnapshot(
        project_id="project-1",
        goal="Ship",
        constraints=(),
        tasks=(failed, waiting),
    )

    projection = project_pm_context(snapshot)

    assert projection.blockers == (failed, waiting)
    assert not hasattr(projection, "thread_id")
    assert not hasattr(projection, "checkpoint_id")
    assert not hasattr(projection, "provider")
