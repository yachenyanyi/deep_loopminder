"""项目查询与修改工具：使用官方 ToolRuntime 绑定项目范围。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, ConfigDict, Field

from .artifacts import ProjectArtifacts
from .commands import (
    CreateTaskCommand,
    ReplaceTaskBriefCommand,
    TransitionTaskCommand,
    UpdateTaskDependenciesCommand,
)
from .context import render_project_context
from .models import ProjectSnapshot, Task, TaskStatus
from .persistence import ProjectStore, _snapshot_to_value
from .scope import STUDIO_GRAPH_ID, resolve_project_id
from .service import apply_project_command
from .transitions import ProjectStateError, task_controls


@dataclass(frozen=True)
class TransitionFacts:
    """宿主针对当前快照和本次状态转换提供的可信结果。"""

    validation_accepted: bool = False
    human_approved: bool = False


FactsProvider = Callable[[ProjectSnapshot, str, TaskStatus], Awaitable[TransitionFacts]]


class TaskDraft(BaseModel):
    """新任务的输入结构：固定从待办开始，不接收验收或审批结果。"""

    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    title: str = Field(min_length=1)
    task_brief_ref: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    owner_role: str | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    priority: int = 0


def project_scope(runtime) -> tuple[str, ProjectStore]:
    """从宿主运行配置中读取项目标识，并绑定官方 Store。"""
    project_id = resolve_project_id(runtime.config)
    if runtime.store is None:
        raise ValueError("project state requires the official runtime Store")
    return project_id, ProjectStore(runtime.store)


def require_writer(runtime) -> None:
    """保存快照前，确认宿主已声明每个项目仅有一个串行写入者。"""
    if runtime.config.get("configurable", {}).get("graph_id") == STUDIO_GRAPH_ID:
        raise ValueError("Project writes are disabled in Studio test mode")
    if (
        runtime.config.get("configurable", {}).get("project_state_single_writer")
        is not True
    ):
        raise ValueError(
            "Project writes disabled: host must guarantee one serial writer per "
            "project and set project_state_single_writer=True"
        )


@tool
async def project_context(runtime: ToolRuntime) -> str:
    """Read the bounded authoritative current project map, not historical memory."""
    project_id, store = project_scope(runtime)
    snapshot = await store.load(project_id)
    return (
        render_project_context(snapshot)
        if snapshot
        else f"Project state not found: {project_id}"
    )


@tool
async def project_read(runtime: ToolRuntime, task_id: str | None = None) -> dict:
    """Read project facts. Supply task_id for one task plus allowed transitions, blockers and recovery guidance. Without task_id returns the full snapshot."""
    project_id, store = project_scope(runtime)
    snapshot = await store.load(project_id)
    if snapshot and task_id is not None:
        controls = task_controls(snapshot, task_id)
        value = next(
            task
            for task in _snapshot_to_value(snapshot)["tasks"]
            if task["task_id"] == task_id
        )
        return {"project_id": project_id, "task": value, "controls": controls}
    return (
        _snapshot_to_value(snapshot)
        if snapshot
        else {"project_id": project_id, "found": False}
    )


def make_project_command(
    facts_provider: FactsProvider | None = None,
    *,
    artifacts: ProjectArtifacts | None = None,
):
    """构建统一的项目修改工具，可信转换依据由宿主提供。"""

    @tool
    async def project_command(
        action: Literal[
            "create_project",
            "update_project",
            "create_task",
            "update_task_brief",
            "transition_task",
            "update_task_dependencies",
        ],
        runtime: ToolRuntime,
        goal: str | None = None,
        constraints: list[str] | None = None,
        task: TaskDraft | None = None,
        task_id: str | None = None,
        target_status: TaskStatus | None = None,
        task_brief_ref: str | None = None,
        dependencies: list[str] | None = None,
    ) -> dict:
        """Mutate the current project using deterministic rules.

        Call one command at a time and read its result before the next command. create_project
        requires goal; update_project replaces supplied goal/constraints;
        create_task requires task (including TaskBrief ref and acceptance criteria);
        transition_task requires task_id/target_status. passed/approved require
        trusted host evidence, never self-reported approval. Runtime binds scope.
        update_task_dependencies requires task_id/dependencies and only edits unexecuted backlog/ready tasks,
        resetting them to backlog. dependencies always wait for DONE; do not make a review depend on its
        已执行但尚未done的前置依赖不能删除，不能用删边绕过验收；protected_dependencies列出受保护的边。
        own subject being DONE. Use spawn_agent with same task_id/new task_name for review/testing.
        On rejection read controls; missing trusted evidence requires host/human action, not status guessing.
        """
        require_writer(runtime)
        project_id, store = project_scope(runtime)
        snapshot = await store.load(project_id)
        if action == "create_project":
            if snapshot is not None:
                raise ValueError("project already exists; use update_project")
            if goal is None:
                raise ValueError("create_project requires goal")
            candidate = ProjectSnapshot(project_id, goal, tuple(constraints or ()), ())
        else:
            if snapshot is None:
                raise ValueError("project not found; use create_project first")
            if action == "update_project":
                if goal is None and constraints is None:
                    raise ValueError("update_project requires goal or constraints")
                candidate = replace(
                    snapshot,
                    goal=goal if goal is not None else snapshot.goal,
                    constraints=tuple(constraints)
                    if constraints is not None
                    else snapshot.constraints,
                )
            elif action == "create_task":
                if task is None:
                    raise ValueError("create_task requires task")
                if artifacts:
                    await artifacts.get(
                        project_id,
                        task.task_brief_ref,
                        task_id=task.task_id,
                        kind="task_brief",
                    )
                candidate = apply_project_command(
                    snapshot,
                    CreateTaskCommand(
                        project_id,
                        Task(
                            task.task_id,
                            task.title,
                            TaskStatus.BACKLOG,
                            task.owner_role,
                            frozenset(task.required_capabilities),
                            frozenset(task.dependencies),
                            tuple(task.acceptance_criteria),
                            task.priority,
                        ),
                        task.task_brief_ref,
                    ),
                )
            elif action == "update_task_brief":
                if task_id is None or task_brief_ref is None or artifacts is None:
                    raise ValueError(
                        "update_task_brief需要task_id、有效制品引用和项目制品服务"
                    )
                await artifacts.get(
                    project_id, task_brief_ref, task_id=task_id, kind="task_brief"
                )
                candidate = apply_project_command(
                    snapshot,
                    ReplaceTaskBriefCommand(project_id, task_id, task_brief_ref),
                )
            elif action == "update_task_dependencies":
                if task_id is None or dependencies is None:
                    raise ValueError(
                        "update_task_dependencies需要task_id和dependencies（空列表表示移除依赖）"
                    )
                candidate = apply_project_command(
                    snapshot,
                    UpdateTaskDependenciesCommand(
                        project_id, task_id, frozenset(dependencies)
                    ),
                )
            else:
                if task_id is None or target_status is None:
                    raise ValueError(
                        "transition_task requires task_id and target_status"
                    )
                facts = TransitionFacts()
                if (
                    target_status in {TaskStatus.PASSED, TaskStatus.APPROVED}
                    or (target_status is TaskStatus.DONE and artifacts is not None)
                ) and facts_provider:
                    facts = await facts_provider(snapshot, task_id, target_status)
                    if not isinstance(facts, TransitionFacts):
                        raise ValueError("facts_provider must return TransitionFacts")
                if (
                    target_status is TaskStatus.DONE
                    and artifacts is not None
                    and not (facts.validation_accepted and facts.human_approved)
                ):
                    raise ProjectStateError(
                        "evidence_required",
                        "当前版本缺少有效验收或人工批准，不能完成任务",
                        "request_human_decision",
                    )
                candidate = apply_project_command(
                    snapshot,
                    TransitionTaskCommand(
                        project_id,
                        task_id,
                        target_status,
                        validation_accepted=facts.validation_accepted,
                        human_approved=facts.human_approved,
                    ),
                )
        await store.save(candidate)
        # 写操作只返回本次修改，完整快照由project_read显式读取，避免每步重复全部任务。
        changed_id = task.task_id if action == "create_task" else task_id
        if changed_id:
            return {
                "ok": True,
                "project_id": project_id,
                "task": next(
                    item
                    for item in _snapshot_to_value(candidate)["tasks"]
                    if item["task_id"] == changed_id
                ),
                "controls": task_controls(candidate, changed_id),
            }
        return {
            "ok": True,
            "project_id": project_id,
            "goal": candidate.goal,
            "constraints": list(candidate.constraints),
            "task_count": len(candidate.tasks),
        }

    return project_command
