"""Deterministic Project/Task dependency and transition policy.

This module owns only DeepLoop business-state invariants. Validation and human
approval are supplied as trusted facts by their owning layers; execution
lifecycle remains owned by LangGraph/Deep Agents/provider interfaces.
"""

from enum import StrEnum

from src.middlewares.project_state.commands import TransitionTaskCommand
from src.middlewares.project_state.models import ProjectSnapshot, TaskStatus


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
    TaskStatus.REVIEW: frozenset({TaskStatus.FAILED, TaskStatus.TESTING}),
    TaskStatus.TESTING: frozenset({TaskStatus.FAILED, TaskStatus.REVIEW}),
    TaskStatus.FAILED: frozenset({TaskStatus.REWORK}),
    TaskStatus.REWORK: frozenset({TaskStatus.REVIEW, TaskStatus.TESTING}),
    TaskStatus.PASSED: frozenset({TaskStatus.WAITING_HUMAN}),
    TaskStatus.APPROVED: frozenset({TaskStatus.DONE}),
}


class ProjectStateError(ValueError):
    """可恢复的业务拒绝，同时提供明确原因与下一步，不捕获基础设施故障。"""

    def __init__(self, code: str, message: str, next_action: str):
        """保存工具可直接展示的错误原因和恢复动作。"""
        super().__init__(message)
        self.details = {"code": code, "reason": message, "next_action": next_action}


def transition_rejection(
    snapshot: ProjectSnapshot, command: TransitionTaskCommand
) -> ProjectStateError:
    """解释策略已经拒绝的转换；是否允许仍由transition_decision唯一决定。"""
    task = next(
        (item for item in snapshot.tasks if item.task_id == command.task_id), None
    )
    if task is None:
        return ProjectStateError(
            "task_not_found",
            "任务不存在，请读取现有编号，不能通过猜测状态修复",
            "project_read",
        )
    if task.status is TaskStatus.BACKLOG and command.target_status is TaskStatus.READY:
        blocked = [
            f"{item.task_id}={item.status.value}"
            for item in snapshot.tasks
            if item.task_id in task.dependencies and item.status is not TaskStatus.DONE
        ]
        return ProjectStateError(
            "dependencies_blocked",
            "依赖尚未完成："
            + ", ".join(blocked)
            + "。错误依赖可在执行前修正，真实前置依赖应等待done，不能为推进而删除。",
            "inspect_dependencies",
        )
    if command.target_status is TaskStatus.PASSED and task.status in {
        TaskStatus.REVIEW,
        TaskStatus.TESTING,
        TaskStatus.PASSED,
    }:
        return ProjectStateError(
            "validation_required",
            "缺少有效的人工验收记录，需要逐项核验后记录结果",
            "request_human_decision",
        )
    if command.target_status is TaskStatus.APPROVED and task.status in {
        TaskStatus.PASSED,
        TaskStatus.WAITING_HUMAN,
        TaskStatus.APPROVED,
    }:
        return ProjectStateError(
            "approval_required",
            "缺少对这份验收记录的人工批准，请请求人工决定",
            "request_human_decision",
        )
    return ProjectStateError(
        "invalid_task_transition",
        f"不能从{task.status.value}转换为{command.target_status.value}；请按controls选择下一步",
        "inspect_controls",
    )


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

    # 重复投递相同状态不再产生拒绝，也不绕过终态的证据复核。
    if source is target:
        accepted = target not in {
            TaskStatus.PASSED,
            TaskStatus.APPROVED,
            TaskStatus.DONE,
        }
        if target is TaskStatus.PASSED:
            accepted = command.validation_accepted is True
        elif target in {TaskStatus.APPROVED, TaskStatus.DONE}:
            accepted = (
                command.validation_accepted is True and command.human_approved is True
            )
        return TransitionDecision.ALLOWED if accepted else TransitionDecision.REJECTED

    if source is TaskStatus.BACKLOG and target is TaskStatus.READY:
        return (
            TransitionDecision.ALLOWED
            if dependency_readiness(snapshot, task.task_id) is DependencyReadiness.READY
            else TransitionDecision.REJECTED
        )

    if (
        source in {TaskStatus.REVIEW, TaskStatus.TESTING}
        and target is TaskStatus.PASSED
    ):
        return (
            TransitionDecision.ALLOWED
            if command.validation_accepted is True
            else TransitionDecision.REJECTED
        )

    if (
        source in {TaskStatus.PASSED, TaskStatus.WAITING_HUMAN}
        and target is TaskStatus.APPROVED
    ):
        return (
            TransitionDecision.ALLOWED
            if command.human_approved is True
            else TransitionDecision.REJECTED
        )

    if target in _PLAIN_TRANSITIONS.get(source, frozenset()):
        return TransitionDecision.ALLOWED
    return TransitionDecision.REJECTED


def task_controls(snapshot: ProjectSnapshot, task_id: str) -> dict:
    """由同一转换策略生成恢复提示，避免文档维护另一套状态机。"""
    task = next((item for item in snapshot.tasks if item.task_id == task_id), None)
    if task is None:
        raise ValueError(f"unknown task_id: {task_id}")
    allowed = [
        status.value
        for status in TaskStatus
        if status is not task.status
        and transition_decision(
            snapshot, TransitionTaskCommand(snapshot.project_id, task_id, status)
        )
        is TransitionDecision.ALLOWED
    ]
    gates = {}
    if task.status in {TaskStatus.REVIEW, TaskStatus.TESTING}:
        gates["passed"] = (
            "需要宿主记录人工逐项验收。缺少记录时，请用户核对并提交验收。"
        )
    if task.status in {TaskStatus.PASSED, TaskStatus.WAITING_HUMAN}:
        gates["approved"] = "需要已通过的人工验收以及对该验收记录作出的人工批准。"
    if task.status is TaskStatus.APPROVED:
        gates["done"] = "完成前需核对人工验收和批准记录。"
    blockers = [
        {"task_id": item.task_id, "status": item.status.value}
        for item in snapshot.tasks
        if item.task_id in task.dependencies and item.status is not TaskStatus.DONE
    ]
    return {
        "task_id": task_id,
        "current_status": task.status.value,
        "allowed_transitions": allowed,
        "gated_transitions": gates,
        "blocking_dependencies": blockers,
        "dependency_editable": task.status in {TaskStatus.BACKLOG, TaskStatus.READY}
        and not task.execution_refs,
        "protected_dependencies": sorted(
            dependency.task_id
            for dependency in snapshot.tasks
            if dependency.task_id in task.dependencies
            and dependency.execution_refs
            and dependency.status is not TaskStatus.DONE
        ),
        "execution_refs": list(task.execution_refs),
        "allowed_worker_roles": sorted(
            ({task.owner_role} if task.owner_role else set())
            | (
                {"reviewer", "tester"}
                if task.status in {TaskStatus.REVIEW, TaskStatus.TESTING}
                else set()
            )
        ),
        "result_collection": "官方完成通知仅是状态；使用wait_agent领取并持久化最终制品。",
        "review_dispatch": "review/testing时可用新的task_name、相同task_id委派reviewer/tester，无需创建依赖本任务done的审查任务。",
    }
