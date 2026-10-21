"""PM Agent 的完整装配：按配置、提示词、上下文、中间件、主图的顺序阅读。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware

from src.middlewares.approval import (
    HumanDecisionRequestMiddleware,
    human_decision_hitl_middleware,
)
from src.middlewares.context_projection import (
    ContextBlock,
    ContextProjection,
    make_context_projection_prompt,
)
from src.middlewares.memory.pm_agent_memory import (
    PMAgentMemoryMiddleware,
    PMMemory,
    get_pm_memory_store,
)
from src.middlewares.memory.pm_agent_memory.lifecycle import (
    PMMemorySummarizationMiddleware,
)
from src.middlewares.project_state import (
    ProjectSnapshot,
    ProjectStateMiddleware,
    Task,
    TaskStatus,
)
from src.middlewares.project_state.service import ProjectCommand as ProjectCommand
from src.middlewares.project_state.service import apply_project_command
from src.runtime.routing import RoleRequirement as RoleRequirement
from src.runtime.routing import role_requirement_from_task

if TYPE_CHECKING:
    from src.middlewares.acp import ACPMiddleware


# 1. PM 配置：声明角色、能力及需要装配的中间件。


@dataclass(frozen=True, slots=True)
class PMProfile:
    """PM 的角色与能力配置，不绑定具体 Agent 提供方。"""

    role: str
    capabilities: frozenset[str]
    context_policy: str
    memory_policy: str
    budget_policy: str
    approval_policy: str
    middleware_profile: tuple[str, ...] = ()


_PM_CONTROL_CAPABILITIES = frozenset(
    {
        "project.read",
        "project.command",
        "project.plan",
        "project.replan",
        "project.delegate.role_requirement",
        "project.request_human_decision",
    }
)

_FORBIDDEN_EXECUTION_CAPABILITIES = frozenset(
    {
        "repo.write.source",
        "shell.execute",
        "test.execute",
        "provider.select",
        "provider.session.control",
    }
)

_PM_MIDDLEWARE_REQUIREMENTS = (
    "HumanDecisionRequestMiddleware",
    "human_decision_hitl_middleware",
    "ProjectStateMiddleware",
    "PMAgentMemoryMiddleware",
)


def default_pm_profile(*, acp_enabled: bool = False) -> PMProfile:
    """生成 PM 默认配置，实际执行能力由 Worker 提供。"""
    profile = PMProfile(
        role="project_manager",
        capabilities=_PM_CONTROL_CAPABILITIES,
        context_policy="pm_project_projection",
        memory_policy="pm_project_orientation_and_recall",
        budget_policy="pm_control_budget",
        approval_policy="human_boundary",
        middleware_profile=_PM_MIDDLEWARE_REQUIREMENTS
        + (("ACPMiddleware",) if acp_enabled else ()),
    )
    forbidden = profile.capabilities & _FORBIDDEN_EXECUTION_CAPABILITIES
    if forbidden:
        raise ValueError(
            f"PM profile contains worker execution capabilities: {sorted(forbidden)}"
        )
    return profile


# 2. 固定提示词：定义 PM 的身份和治理规则；实时项目数据由中间件补充。

PM_STABLE_SYSTEM_PROMPT = """You are the Project Control Agent.

Your responsibility is to understand the human goal, plan and replan project
work, decompose work into bounded tasks, reason about dependencies and
blockers, and delegate by role plus required capability.

你是一名负责协调的项目经理。理解客户想要的业务结果，必要时拆成功能，
把目标、范围、优先级和验收要求讲清楚，再安排负责人、跟进进度和阻塞，
汇总开发与测试人员的结论，向用户说明需要决定的事情。
开发人员负责技术调查、实现方案和具体细节；测试与核验人员负责检查交付。
不要代替他们做技术工作，也不要用技术细节充当项目管理成果。

协作方式：
- 公开分析、进度说明、派工、追问和最终回复默认使用中文；要求 Worker 用中文汇报，命令、路径、标识符和原始错误保留原文。
- 将 Worker 当作能自主工作的负责人：交代业务目标、范围、必要约束和完成标准，技术方案与执行细节由它决定。
- 首次派工的完整 TaskBrief 由宿主自动注入，message 只写本轮重点，不重复粘贴简报，也不要求 Worker 调用项目制品工具读取简报。
- 同一 Worker 会话续接时，结合最近交付判断它已知什么，只说明变化、优先级和待完成项；不重复背景、不因上次失败猜测原因后追加永久限制。
- Worker 拥有独立工具集，不继承你的项目状态、记忆、制品工具。通过会话返回进度与成果，由 wait_agent 领取并自动保存；不能要求它研究宿主代码、访问宿主 Store 或绕过工具补能力。
- Worker 若具备原生子代理，可自主拆分内部工作并对最终成果负责；你管理项目级负责人，不指挥其内部子代理。
- 正常跟进使用 wait_agent 默认的新结果模式；只有明确查阅某一历史轮次时，才给出 run_id 重读。不要反复读取旧失败来判断新 Run。

Stable governance principles:
- Treat the human goal as the outcome to achieve, not as a task list or a requested implementation.
- Prefer evidence over status claims, activity counts, or apparent progress.
- Distinguish known facts, assumptions, and unknowns. Do not silently fill material unknowns.
- Plans serve the goal. Change the route when new evidence invalidates important assumptions.
- Prioritize the blocker, risk, or uncertainty with the greatest effect on reaching the goal.
- Keep project-control output purposeful; do not create process artifacts merely for completeness.
- 面向用户默认给出简短的需求理解、安排和关键待确认项，不为普通沟通生成长篇技术报告。
- 功能拆解保持业务层次，不自行指定数据库、接口、代码结构、组件或实现路线。
- 客户没有确认的排期、范围和方案仍是待确认项，不把建议写成承诺。

Hard rules:
- Runtime Project State is the source of truth; conversation text is not.
- Delegate by role and capability. Never select or bind a concrete provider.
- Worker execution completion is not task validation or permission to close a task.
- If required evidence is missing, keep the relevant conclusion UNKNOWN.
- Do not write product source code, execute shell commands, or run tests.
- 不读取或浏览项目目录、源码、代码差异和原始技术日志，也不通过记忆或制品工具绕道审查这些内容。
- 项目技术调查交给调查角色，实现细节交给开发角色，测试和技术核验交给对应角色。
- 只读取管理所需的项目状态、需求、决定、历史管理记忆及 Worker 的管理摘要；技术证据由负责核验的 Worker 检查并汇报。
- Worker 不可用或本轮禁止委派时，只基于客户材料说明需求、安排建议和未知项，不自行补做技术调查。
- Mutate project state only through typed ProjectCommand operations.
- Decisions outside your authority must enter the Human Gate.
- Never weaken acceptance criteria in order to claim completion.
- Create a TaskBrief with project_create_artifact before create_task; use its returned reference, never invent paths.
- 业务task_id和Worker别名区分大小写，使用字母开头的字母/数字/下划线/连字符，最长64字符。保留现有编号，不为大小写另建任务。
- spawn_agent默认以task_name绑定同名业务任务；独立审查/测试使用新的task_name、原业务task_id和reviewer/tester角色，仅在review/testing阶段允许。
- 依赖满足、角色和任务说明有效时，可直接派工backlog/ready任务；官方Run创建成功后工具自动推进分配和进行中状态，无需手动补齐中间状态。前置依赖不满足仍禁止派工。
- dependencies只在依赖任务done后满足。审查工作不能依赖被审查任务先done；未执行任务可用update_task_dependencies修正，不重建同名任务。
- 按工具controls中的allowed_transitions推进；gated_transitions由实际可信记录检查。passed/approved/done缺少可信记录时请求宿主/人工决定，不猜状态或反复重试。
- 官方后台完成通知仅说明Run状态；必须wait_agent领取结果并关联制品，才能用于项目交付依据。
- Read delivered management summaries with project_read_artifact; request a responsible worker's summary when an artifact contains technical source material. Run success never writes validation or human approval.
- If the trusted validation/approval receipt is missing or stale, report the blocker and request the host/human decision.
"""


def stable_pm_system_prompt() -> str:
    """读取固定身份与规则提示词，不在这里写入动态项目事实。"""
    return PM_STABLE_SYSTEM_PROMPT


# 3. 上下文整理：提取 PM 关心的任务和阻塞，供需要投影的调用方使用。

_TERMINAL_TASK_STATUSES = frozenset({TaskStatus.DONE})

_BLOCKED_TASK_STATUSES = frozenset(
    {TaskStatus.FAILED, TaskStatus.REWORK, TaskStatus.WAITING_HUMAN}
)


@dataclass(frozen=True, slots=True)
class PMContextProjection:
    """从当前项目快照整理出的 PM 视图。"""

    project_id: str
    goal: str
    constraints: tuple[str, ...]
    active_tasks: tuple[Task, ...]
    blockers: tuple[Task, ...]


def project_pm_context(snapshot: ProjectSnapshot) -> PMContextProjection:
    """提取目标、约束、活动任务和阻塞，不修改项目状态。"""
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
    """将 PM 视图转换为带优先级的上下文块，供官方投影接口使用。"""
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


# 复用项目命令和任务角色转换，不再实现第二套状态规则。
pm_project_command = apply_project_command
pm_role_requirement = role_requirement_from_task

# 4. 中间件装配：检查配置，依次接入审批、项目状态、记忆、摘要和 ACP。

_PM_CONTEXT_POLICY = "pm_project_projection"

_REVIEWED_PM_REQUIREMENTS = _PM_MIDDLEWARE_REQUIREMENTS


def assemble_pm_user_middleware(
    profile: PMProfile,
    *,
    memory_jobs: PMMemory | None = None,
    acp: ACPMiddleware | None = None,
    project_state: ProjectStateMiddleware | None = None,
    summary_model=None,
    compaction_token_threshold: int = 300_000,
    context_token_limit: int = 500_000,
    keep_messages: int = 20,
    recent_daily_days: int = 3,
    trajectory: AgentMiddleware | None = None,
) -> tuple[AgentMiddleware, ...]:
    """检查 PM 配置并装配中间件，运行生命周期交给官方接口。"""
    if profile.context_policy != _PM_CONTEXT_POLICY:
        raise ValueError("PM context_policy must use the reviewed #33 projection path")
    requirements = _REVIEWED_PM_REQUIREMENTS + (
        ("ACPMiddleware",) if acp is not None else ()
    )
    if profile.middleware_profile != requirements:
        raise ValueError(
            "PM middleware_profile must exactly match the reviewed PM requirements"
        )

    # 基础提示词先注入，再接入人工审批、项目状态和历史记忆。
    middleware = (
        make_context_projection_prompt(stable_pm_system_prompt()),
        HumanDecisionRequestMiddleware(),
        human_decision_hitl_middleware(),
        project_state if project_state is not None else ProjectStateMiddleware(),
        PMAgentMemoryMiddleware(
            memory_jobs,
            recent_daily_days=recent_daily_days,
            context_token_threshold=compaction_token_threshold // 2,
            context_token_limit=context_token_limit,
        ),
    )
    # 配置后台记忆服务后，追加压缩交接，避免长对话丢失关键依据。
    if memory_jobs is not None:
        middleware += (
            PMMemorySummarizationMiddleware(
                memory_jobs,
                summary_model or memory_jobs.model,
                token_threshold=compaction_token_threshold,
                keep_messages=keep_messages,
            ),
        )
    # ACP 是可选能力；启用后才向 PM 暴露子代理工具。
    if acp is not None:
        middleware += (acp,)
    # 轨迹只做观察，置于最外层才能收到业务中间件最终返回的错误。
    if trajectory is not None:
        # 外层观察最终错误，内层采集经过记忆/上下文注入的实际模型请求。
        return (trajectory, *middleware, trajectory.request_capture())
    return middleware


# 5. 创建主图：读取模型和运行配置，调用官方 create_agent 完成组装。


async def pm_agent():
    """创建 PM 主图，运行与检查点生命周期交给官方 Server。"""
    from src.models.llm import get_default_model

    # 先确定模型与共享存储，项目状态和历史记忆使用同一个 Store。
    model = get_default_model()
    store = (await get_pm_memory_store()).store
    memory = PMMemory(store, model)
    from src.middlewares.project_state.workflow import ProjectWorkflow

    project_state = ProjectStateMiddleware(workflow=ProjectWorkflow(store))
    trajectory = None
    if directory := os.getenv("PM_TRAJECTORY_DIRECTORY"):
        from src.middlewares.trajectory import TrajectoryMiddleware, TrajectoryRecorder

        trajectory = TrajectoryMiddleware(
            TrajectoryRecorder(directory),
            stop_on_error=os.getenv("PM_TRAJECTORY_STOP_ON_ERROR", "false").lower() == "true",
        )
    # 根据部署配置启用 ACP；角色与 Worker 连接仍由 ACP 中间件管理。
    specs = json.loads(os.getenv("ACP_SUBAGENTS_JSON") or "[]")
    acp = None
    if specs:
        from src.middlewares.acp import ACPMiddleware
        from src.middlewares.acp.server import worker_catalog

        acp = ACPMiddleware(
            async_subagents=specs,
            candidate_source=worker_catalog if all(not spec.get("url") for spec in specs) else None,
        )
    # 最后把身份提示词、已装配的中间件和 Store 交给官方 Agent 工厂。
    return create_agent(
        model,
        system_prompt=stable_pm_system_prompt(),
        middleware=assemble_pm_user_middleware(
            default_pm_profile(acp_enabled=acp is not None),
            acp=acp,
            memory_jobs=memory,
            project_state=project_state,
            trajectory=trajectory,
            compaction_token_threshold=int(
                os.getenv("PM_COMPACTION_TOKEN_THRESHOLD") or "300000"
            ),
            context_token_limit=int(os.getenv("PM_CONTEXT_TOKEN_LIMIT") or "500000"),
        ),
        store=store,
    )
