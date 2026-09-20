"""Tests that PM mutations stay on the #36 typed command boundary."""

from src.runtime.pm import ProjectCommand, pm_project_command
from src.runtime.project import CreateTaskCommand, ProjectSnapshot, Task, TaskStatus
from src.runtime.project.service import apply_project_command


def test_pm_uses_project_domain_reducer_without_parallel_mutation_api() -> None:
    assert pm_project_command is apply_project_command


def test_pm_surface_accepts_the_existing_typed_project_command() -> None:
    snapshot = ProjectSnapshot(
        project_id="project-1",
        goal="Ship",
        constraints=(),
        tasks=(),
    )
    task = Task(
        task_id="task-1",
        title="Implement",
        status=TaskStatus.READY,
        owner_role="developer",
        required_capabilities=frozenset({"python"}),
        dependencies=frozenset(),
        acceptance_criteria=("validated",),
    )
    command: ProjectCommand = CreateTaskCommand(
        project_id=snapshot.project_id,
        task=task,
        task_brief_ref="artifact://task-brief/1",
    )

    updated = pm_project_command(snapshot, command)

    assert updated.tasks[0].task_id == "task-1"
    assert "artifact://task-brief/1" in updated.tasks[0].artifact_refs
