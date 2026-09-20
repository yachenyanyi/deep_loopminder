"""Deterministic Project/Task dependency and transition policy.

This module owns only DeepLoop business-state invariants. Validation and human
approval are supplied as trusted facts by their owning layers; execution
lifecycle remains owned by LangGraph/Deep Agents/provider interfaces.
"""

from enum import StrEnum

from src.runtime.project.commands import TransitionTaskCommand
from src.runtime.project.models import ProjectSnapshot, TaskStatus


class DependencyReadiness(StrEnum):
    """Whether all declared project-task dependencies are complete."""

    READY = "ready"
    BLOCKED = "blocked"


class TransitionDecision(StrEnum):
    """Result of evaluating a project-business status transition."""

    ALLOWED = "allowed"
    REJECTED = "rejected"


_PLAIN_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.READY: frozenset({TaskStatus.ASSIGNED}),
    TaskStatus.ASSIGNED: frozenset({TaskStatus.IN_PROGRESS}),
    TaskStatus.IN_PROGRESS: frozenset({TaskStatus.DEV_COMPLETE, TaskStatus.FAILED}),
    TaskStatus.DEV_COMPLETE: frozenset({TaskStatus.REVIEW, TaskStatus.TESTING}),
    TaskStatus.REVIEW: frozenset({TaskStatus.FAILED}),
    TaskStatus.TESTING: frozenset({TaskStatus.FAILED}),
    TaskStatus.FAILED: frozenset({TaskStatus.REWORK}),
    TaskStatus.REWORK: frozenset({TaskStatus.REVIEW, TaskStatus.TESTING}),
    TaskStatus.PASSED: frozenset({TaskStatus.WAITING_HUMAN}),
    TaskStatus.APPROVED: frozenset({TaskStatus.DONE}),
}


def validate_dependency_graph(snapshot: ProjectSnapshot) -> None:
    """Reject missing dependency identities and dependency cycles."""
    tasks = {task.task_id: task for task in snapshot.tasks}
    for task in snapshot.tasks:
        missing = sorted(task.dependencies - tasks.keys())
        if missing:
            raise ValueError(
                f"task {task.task_id!r} has unknown dependencies: {missing!r}"
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise ValueError("project task dependencies must be acyclic")
        visiting.add(task_id)
        for dependency_id in tasks[task_id].dependencies:
            visit(dependency_id)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)


def dependency_readiness(
    snapshot: ProjectSnapshot,
    task_id: str,
) -> DependencyReadiness:
    """Compute ready/blocked from deterministic project-domain facts."""
    validate_dependency_graph(snapshot)
    tasks = {task.task_id: task for task in snapshot.tasks}
    try:
        task = tasks[task_id]
    except KeyError as exc:
        raise ValueError(f"unknown task_id: {task_id}") from exc

    if all(tasks[dep].status is TaskStatus.DONE for dep in task.dependencies):
        return DependencyReadiness.READY
    return DependencyReadiness.BLOCKED


def transition_decision(
    snapshot: ProjectSnapshot,
    command: TransitionTaskCommand,
) -> TransitionDecision:
    """Authorize only explicit business transitions backed by required facts."""
    if command.project_id != snapshot.project_id:
        return TransitionDecision.REJECTED

    tasks = {task.task_id: task for task in snapshot.tasks}
    task = tasks.get(command.task_id)
    if task is None:
        return TransitionDecision.REJECTED

    source = task.status
    target = command.target_status

    if source is TaskStatus.BACKLOG and target is TaskStatus.READY:
        return (
            TransitionDecision.ALLOWED
            if dependency_readiness(snapshot, task.task_id) is DependencyReadiness.READY
            else TransitionDecision.REJECTED
        )

    if source in {TaskStatus.REVIEW, TaskStatus.TESTING} and target is TaskStatus.PASSED:
        return (
            TransitionDecision.ALLOWED
            if command.validation_accepted is True
            else TransitionDecision.REJECTED
        )

    if source in {TaskStatus.PASSED, TaskStatus.WAITING_HUMAN} and target is TaskStatus.APPROVED:
        return (
            TransitionDecision.ALLOWED
            if command.human_approved is True
            else TransitionDecision.REJECTED
        )

    if target in _PLAIN_TRANSITIONS.get(source, frozenset()):
        return TransitionDecision.ALLOWED
    return TransitionDecision.REJECTED
