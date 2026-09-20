"""Pure application service for typed project-domain commands.

The service mutates only immutable DeepLoop business snapshots. Persistence,
execution lifecycle, streams, checkpoints, traces, and artifact bytes remain
owned by their official LangGraph/Deep Agents/provider interfaces.
"""

from dataclasses import replace

from src.runtime.project.commands import (
    CreateTaskCommand,
    TransitionTaskCommand,
    UpdateAcceptanceCriteriaCommand,
)
from src.runtime.project.models import ProjectSnapshot, Task
from src.runtime.project.transitions import (
    TransitionDecision,
    transition_decision,
    validate_dependency_graph,
)

ProjectCommand = (
    CreateTaskCommand | TransitionTaskCommand | UpdateAcceptanceCriteriaCommand
)


def apply_project_command(
    snapshot: ProjectSnapshot,
    command: ProjectCommand,
) -> ProjectSnapshot:
    """Apply one authorized business command and return a new snapshot."""
    if command.project_id != snapshot.project_id:
        raise ValueError("command project_id does not match snapshot")
    if isinstance(command, CreateTaskCommand):
        return _create_task(snapshot, command)
    if isinstance(command, TransitionTaskCommand):
        return _transition_task(snapshot, command)
    return _update_acceptance_criteria(snapshot, command)


def _create_task(
    snapshot: ProjectSnapshot,
    command: CreateTaskCommand,
) -> ProjectSnapshot:
    if any(task.task_id == command.task.task_id for task in snapshot.tasks):
        raise ValueError(f"duplicate task_id: {command.task.task_id}")

    task = replace(
        command.task,
        artifact_refs=_append_unique(
            command.task.artifact_refs,
            command.task_brief_ref,
        ),
    )
    candidate = replace(snapshot, tasks=(*snapshot.tasks, task))
    validate_dependency_graph(candidate)
    return candidate


def _transition_task(
    snapshot: ProjectSnapshot,
    command: TransitionTaskCommand,
) -> ProjectSnapshot:
    if transition_decision(snapshot, command) is not TransitionDecision.ALLOWED:
        raise ValueError("task status transition rejected by project policy")
    return _replace_task(
        snapshot,
        command.task_id,
        replace(_task(snapshot, command.task_id), status=command.target_status),
    )


def _update_acceptance_criteria(
    snapshot: ProjectSnapshot,
    command: UpdateAcceptanceCriteriaCommand,
) -> ProjectSnapshot:
    task = _task(snapshot, command.task_id)
    current = set(task.acceptance_criteria)
    proposed = set(command.acceptance_criteria)
    if not current.issubset(proposed):
        raise ValueError("acceptance criteria cannot be removed or weakened")
    return _replace_task(
        snapshot,
        command.task_id,
        replace(task, acceptance_criteria=command.acceptance_criteria),
    )


def _task(snapshot: ProjectSnapshot, task_id: str) -> Task:
    for task in snapshot.tasks:
        if task.task_id == task_id:
            return task
    raise ValueError(f"unknown task_id: {task_id}")


def _replace_task(
    snapshot: ProjectSnapshot,
    task_id: str,
    replacement: Task,
) -> ProjectSnapshot:
    tasks = tuple(
        replacement if task.task_id == task_id else task
        for task in snapshot.tasks
    )
    return replace(snapshot, tasks=tasks)


def _append_unique(values: tuple[str, ...], value: str) -> tuple[str, ...]:
    if value in values:
        return values
    return (*values, value)
