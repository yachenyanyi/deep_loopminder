"""Pure deterministic reducer for typed project-domain commands.

The reducer mutates only immutable DeepLoop business snapshots. It is not an
authorization boundary: callers must supply commands only from their owning
trusted policy/validation layers. Persistence, execution lifecycle, streams,
checkpoints, traces, and artifact bytes remain owned by official interfaces.
"""

from dataclasses import replace

from src.middlewares.project_state.commands import (
    AttachTaskEvidenceCommand,
    CreateTaskCommand,
    ReplaceTaskBriefCommand,
    TransitionTaskCommand,
    UpdateTaskDependenciesCommand,
)
from src.middlewares.project_state.models import ProjectSnapshot, Task, TaskStatus
from src.middlewares.project_state.transitions import (
    TransitionDecision,
    transition_decision,
    transition_rejection,
    validate_dependency_graph,
)

ProjectCommand = (
    CreateTaskCommand
    | TransitionTaskCommand
    | AttachTaskEvidenceCommand
    | ReplaceTaskBriefCommand
    | UpdateTaskDependenciesCommand
)


def apply_project_command(
    snapshot: ProjectSnapshot,
    command: ProjectCommand,
) -> ProjectSnapshot:
    """Deterministically reduce one domain command into a new snapshot."""
    if command.project_id != snapshot.project_id:
        raise ValueError("command project_id does not match snapshot")
    if isinstance(command, CreateTaskCommand):
        return _create_task(snapshot, command)
    if isinstance(command, AttachTaskEvidenceCommand):
        task = _task(snapshot, command.task_id)
        return _replace_task(
            snapshot,
            task.task_id,
            replace(
                task,
                execution_refs=tuple(
                    dict.fromkeys((*task.execution_refs, *command.execution_refs))
                ),
                artifact_refs=tuple(
                    dict.fromkeys((*task.artifact_refs, *command.artifact_refs))
                ),
            ),
        )
    if isinstance(command, UpdateTaskDependenciesCommand):
        task = _task(snapshot, command.task_id)
        if (
            task.status not in {TaskStatus.BACKLOG, TaskStatus.READY}
            or task.execution_refs
        ):
            raise ValueError(
                "只能修改尚未执行的backlog/ready任务依赖；执行历史不能重写"
            )
        candidate = _replace_task(
            snapshot,
            task.task_id,
            replace(task, dependencies=command.dependencies, status=TaskStatus.BACKLOG),
        )
        validate_dependency_graph(candidate)
        return candidate
    if isinstance(command, ReplaceTaskBriefCommand):
        task = _task(snapshot, command.task_id)
        if task.status.value in {"done", "approved"}:
            raise ValueError("已批准或完成的任务不能替换任务说明")
        return _replace_task(
            snapshot,
            task.task_id,
            replace(
                task, artifact_refs=(command.task_brief_ref, *task.artifact_refs[1:])
            ),
        )
    return _transition_task(snapshot, command)


def _create_task(
    snapshot: ProjectSnapshot,
    command: CreateTaskCommand,
) -> ProjectSnapshot:
    if any(task.task_id == command.task.task_id for task in snapshot.tasks):
        raise ValueError(
            f"duplicate task_id: {command.task.task_id}；请读取现有任务。依赖修正使用update_task_dependencies，说明修正使用update_task_brief，不能重建覆盖"
        )

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
        raise transition_rejection(snapshot, command)
    return _replace_task(
        snapshot,
        command.task_id,
        replace(_task(snapshot, command.task_id), status=command.target_status),
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
        replacement if task.task_id == task_id else task for task in snapshot.tasks
    )
    return replace(snapshot, tasks=tasks)


def _append_unique(values: tuple[str, ...], value: str) -> tuple[str, ...]:
    if value in values:
        return values
    return (*values, value)
