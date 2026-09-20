import pytest

from src.runtime.project.commands import (
    CreateTaskCommand,
    TransitionTaskCommand,
    UpdateAcceptanceCriteriaCommand,
)
from src.runtime.project.models import ProjectSnapshot, Task, TaskStatus
from src.runtime.project.service import apply_project_command


def make_task(
    task_id: str,
    status: TaskStatus = TaskStatus.BACKLOG,
    *,
    dependencies: frozenset[str] = frozenset(),
    acceptance_criteria: tuple[str, ...] = ("original criterion",),
) -> Task:
    return Task(
        task_id=task_id,
        title=task_id,
        status=status,
        owner_role=None,
        required_capabilities=frozenset(),
        dependencies=dependencies,
        acceptance_criteria=acceptance_criteria,
    )


def make_snapshot(*tasks: Task) -> ProjectSnapshot:
    return ProjectSnapshot(
        project_id="project-1",
        goal="Ship safely",
        constraints=(),
        tasks=tasks,
    )


def test_create_task_atomically_associates_task_brief_ref() -> None:
    snapshot = make_snapshot()
    command = CreateTaskCommand(
        project_id="project-1",
        task=make_task("task-1"),
        task_brief_ref="artifact://task-brief-1",
    )

    updated = apply_project_command(snapshot, command)

    assert snapshot.tasks == ()
    assert updated.tasks[0].task_id == "task-1"
    assert updated.tasks[0].artifact_refs == ("artifact://task-brief-1",)


def test_create_task_rejects_unknown_dependency() -> None:
    command = CreateTaskCommand(
        project_id="project-1",
        task=make_task("task-1", dependencies=frozenset({"missing"})),
        task_brief_ref="artifact://task-brief-1",
    )

    with pytest.raises(ValueError, match="unknown dependencies"):
        apply_project_command(make_snapshot(), command)


def test_transition_is_applied_only_through_policy() -> None:
    snapshot = make_snapshot(make_task("task-1", TaskStatus.READY))
    command = TransitionTaskCommand(
        project_id="project-1",
        task_id="task-1",
        target_status=TaskStatus.ASSIGNED,
    )

    updated = apply_project_command(snapshot, command)

    assert snapshot.tasks[0].status is TaskStatus.READY
    assert updated.tasks[0].status is TaskStatus.ASSIGNED


def test_rejected_transition_does_not_mutate_snapshot() -> None:
    snapshot = make_snapshot(make_task("task-1", TaskStatus.IN_PROGRESS))
    command = TransitionTaskCommand(
        project_id="project-1",
        task_id="task-1",
        target_status=TaskStatus.DONE,
    )

    with pytest.raises(ValueError, match="transition rejected"):
        apply_project_command(snapshot, command)

    assert snapshot.tasks[0].status is TaskStatus.IN_PROGRESS


def test_acceptance_criteria_can_be_strengthened_additively() -> None:
    snapshot = make_snapshot(make_task("task-1"))
    command = UpdateAcceptanceCriteriaCommand(
        project_id="project-1",
        task_id="task-1",
        acceptance_criteria=("original criterion", "new criterion"),
    )

    updated = apply_project_command(snapshot, command)

    assert updated.tasks[0].acceptance_criteria == (
        "original criterion",
        "new criterion",
    )


def test_acceptance_criteria_cannot_be_lowered() -> None:
    snapshot = make_snapshot(
        make_task(
            "task-1",
            acceptance_criteria=("original criterion", "security criterion"),
        )
    )
    command = UpdateAcceptanceCriteriaCommand(
        project_id="project-1",
        task_id="task-1",
        acceptance_criteria=("original criterion",),
    )

    with pytest.raises(ValueError, match="cannot be removed or weakened"):
        apply_project_command(snapshot, command)


def test_command_cannot_mutate_another_project() -> None:
    snapshot = make_snapshot(make_task("task-1", TaskStatus.READY))
    command = TransitionTaskCommand(
        project_id="other-project",
        task_id="task-1",
        target_status=TaskStatus.ASSIGNED,
    )

    with pytest.raises(ValueError, match="project_id does not match"):
        apply_project_command(snapshot, command)
