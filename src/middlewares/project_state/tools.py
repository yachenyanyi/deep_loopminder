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
    UpdateTaskCapabilitiesCommand,
    UpdateTaskDependenciesCommand,
    UpdateTaskOwnerRoleCommand,
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
    task_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$", description="任务 task_id，以字母开头的唯一标识")
    title: str = Field(min_length=1, description="简洁任务标题")
    task_brief_ref: str = Field(min_length=1, description="project_create_artifact 返回的任务简报制品引用")
    acceptance_criteria: list[str] = Field(min_length=1, description="可逐项核验的验收标准")
    # Routing identifier only; a display name such as "开发负责人" routes to no
    # worker and would leave the task permanently blocked.
    owner_role: Literal["developer", "reviewer", "tester", "researcher"] | None = Field(default=None, description="仅填 developer/reviewer/tester/researcher 角色标识，不填显示名称")
    # Exact-match routing requirements drawn from the provider capability
    # vocabulary; free text here makes the task unroutable.
    required_capabilities: list[
        Literal[
            "repo.read",
            "repo.write",
            "files.read",
            "files.write",
            "shell.execute",
            "test.execute",
            "web.search",
        ]
    ] = Field(default_factory=list, description="精确能力标识，无额外能力要求时留空")
    dependencies: list[str] = Field(default_factory=list, description="须先完成 DONE 的依赖任务 task_id")
    priority: int = Field(default=0, description="任务优先级数值")


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
    """读取最新权威项目事实的主入口。回答进度、验收/批准状态或派工前必须先调用。task_id 可限定单个任务及其允许操作。"""
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


async def requires_discussion(workflow, project_id, task, configurable):
    """会话开关及任务的持久化商讨记录共同决定门禁。"""
    if configurable.get("pm_discussion_mode") is True:
        return True
    from src.deep_agents.agents.pm_agent.discussion import DiscussionGate

    if await DiscussionGate(workflow.store).latest(project_id, task.task_id):
        return True
    for reference in task.execution_refs:
        item = await workflow.store.aget(
            ("deep_loopminder", "project_execution_refs", project_id), reference.rsplit("/", 1)[-1]
        )
        if item and item.value.get("phase") == "discussion":
            return True
    return False


def make_project_read(workflow=None, *, pm_discussion_gate=False):
    """绑定工作流事实，保持无工作流调用方的原快照契约。"""
    if workflow is None:
        return project_read

    @tool("project_read")
    async def read(runtime: ToolRuntime, task_id: str | None = None) -> dict:
        """读取最新权威项目事实的主入口。回答进度、验收/批准状态或派工前必须先调用。省略 task_id 读取项目快照、各任务阶段、商讨门禁、验收审查及未领取运行；指定 task_id 读取该任务与允许操作。"""
        project_id, store = project_scope(runtime)
        snapshot = await store.load(project_id)
        configured = runtime.config.get("configurable", {})
        from src.deep_agents.agents.pm_agent.discussion import DiscussionGate

        gate = DiscussionGate(runtime.store)
        facts = await workflow.review_status(snapshot, include_all_tasks=True) if snapshot else []
        for fact in facts:
            identity = fact["task_id"]
            review = await workflow.independent_review_status(project_id, identity)
            discussion = await gate.latest(project_id, identity)
            task = next(task for task in snapshot.tasks if task.task_id == identity)
            in_discussion = pm_discussion_gate and await requires_discussion(workflow, project_id, task, configured)
            passed = False
            if discussion:
                try:
                    await gate.require_pass(project_id, identity, worker_agent_id=discussion.get("worker_agent_id", ""))
                    passed = True
                except ValueError:
                    pass
            phase = "商讨已过·可开发" if passed else "待商讨" if in_discussion else "可直接委派"
            if task.status == TaskStatus.IN_PROGRESS:
                phase = "开发中"
            if task.status in {TaskStatus.DEV_COMPLETE, TaskStatus.REVIEW, TaskStatus.TESTING}:
                phase = "待人工验收" if review.get("status") == "pass" else "待审查"
            if task.status in {TaskStatus.PASSED, TaskStatus.WAITING_HUMAN, TaskStatus.APPROVED}:
                phase = "待人工验收"
            if task.status == TaskStatus.DONE:
                phase = "已完成"
            discussion_state = {"status": "not_recorded"} if discussion is None else {
                "decision_id": discussion.get("decision_id"), "decision": discussion.get("decision"),
                "current_pass": passed, "worker_agent_id": discussion.get("worker_agent_id"),
            }
            fact.update(independent_review=review, discussion=discussion_state,
                        discussion_mode=in_discussion, clef_passed=passed, phase_label=phase)
        unclaimed = [task["run_id"] for task in runtime.state.get("async_tasks", {}).values()
                     if task.get("status") in {"success", "error", "interrupted", "timeout", "cancelled"}
                     and not runtime.state.get("acp_delivered", {}).get(task["run_id"])]
        common = {"discussion_mode": configured.get("pm_discussion_mode") is True,
                  "unclaimed_runs": unclaimed}
        if task_id is not None:
            result = await project_read.coroutine(runtime=runtime, task_id=task_id)
            fact = next((fact for fact in facts if fact["task_id"] == task_id), {})
            return {**result, **common, "review_fact": fact,
                    "independent_review": fact.get("independent_review"), "discussion": fact.get("discussion")}
        return {"snapshot": _snapshot_to_value(snapshot) if snapshot else None,
                **common, "review_facts": facts}

    return read


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
            "update_task_owner_role",
            "update_task_capabilities",
        ],
        runtime: ToolRuntime,
        goal: str | None = None,
        constraints: list[str] | None = None,
        task: TaskDraft | None = None,
        task_id: str | None = None,
        target_status: TaskStatus | None = None,
        task_brief_ref: str | None = None,
        dependencies: list[str] | None = None,
        owner_role: Literal["developer", "reviewer", "tester", "researcher"] | None = None,
        required_capabilities: list[
            Literal[
                "repo.read",
                "repo.write",
                "files.read",
                "files.write",
                "shell.execute",
                "test.execute",
                "web.search",
            ]
        ] | None = None,
    ) -> dict:
        """逐次修改当前项目并读取结果，禁止混批或自行伪造验收与批准。

        8 个 action 及必填参数：create_project 需 goal；update_project 需 goal 或 constraints；
        create_task 需 task（含 task_id、task_brief_ref、acceptance_criteria）；
        update_task_brief 需 task_id、task_brief_ref；transition_task 需 task_id、target_status；
        update_task_dependencies 需 task_id、dependencies；update_task_owner_role 需 task_id、owner_role；
        update_task_capabilities 需 task_id、required_capabilities。
        owner_role 只能填 developer/reviewer/tester/researcher，required_capabilities 无额外要求时留空。
        依赖任务必须完成 DONE，审查用 begin_review。依赖、角色和能力修正只允许未执行的任务。
        passed/approved/done 需要宿主可信人工依据。被拒绝时读取 controls，不猜状态。
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
            elif action == "update_task_owner_role":
                if task_id is None or owner_role is None:
                    raise ValueError(
                        "update_task_owner_role需要task_id和owner_role（developer/reviewer/tester/researcher）"
                    )
                candidate = apply_project_command(
                    snapshot,
                    UpdateTaskOwnerRoleCommand(project_id, task_id, owner_role),
                )
            elif action == "update_task_capabilities":
                if task_id is None or required_capabilities is None:
                    raise ValueError(
                        "update_task_capabilities需要task_id和required_capabilities（可用repo.read/repo.write/files.read/files.write/shell.execute/test.execute/web.search）"
                    )
                candidate = apply_project_command(
                    snapshot,
                    UpdateTaskCapabilitiesCommand(
                        project_id, task_id, frozenset(required_capabilities)
                    ),
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
                        "缺少有效的人工验收或批准记录，不能完成任务",
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
