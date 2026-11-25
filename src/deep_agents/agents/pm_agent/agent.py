"""PM Agent 的完整装配：按配置、提示词、上下文、中间件、主图的顺序阅读。"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware

from src.deep_agents.agents.pm_agent.discussion import PMDiscussionMiddleware
from src.middlewares.agent.pm_settings import (
    resolve_compaction_settings as resolve_compaction_settings,
)
from src.middlewares.agent.user_configurable import PMModelSelectionMiddleware
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
    "PMModelSelectionMiddleware",
    "PMDiscussionMiddleware",
    "HumanDecisionRequestMiddleware",
    "human_decision_hitl_middleware",
    "ProjectStateMiddleware",
    "PMAgentMemoryMiddleware",
)


def default_pm_profile(*, acp_enabled: bool = False, memory_enabled: bool = True) -> PMProfile:
    """生成 PM 默认配置，实际执行能力由 Worker 提供。"""
    profile = PMProfile(
        role="project_manager",
        capabilities=_PM_CONTROL_CAPABILITIES,
        context_policy="pm_project_projection",
        memory_policy="pm_project_orientation_and_recall" if memory_enabled else "disabled",
        budget_policy="pm_control_budget",
        approval_policy="human_boundary",
        middleware_profile=tuple(name for name in _PM_MIDDLEWARE_REQUIREMENTS
                                 if memory_enabled or name != "PMAgentMemoryMiddleware")
        + (("ACPMiddleware",) if acp_enabled else ()),
    )
    forbidden = profile.capabilities & _FORBIDDEN_EXECUTION_CAPABILITIES
    if forbidden:
        raise ValueError(
            f"PM profile contains worker execution capabilities: {sorted(forbidden)}"
        )
    return profile


# 2. 固定提示词：定义 PM 的身份和治理规则；实时项目数据由中间件补充。

from .prompts import build_pm_system_prompt


def stable_pm_system_prompt() -> str:
    """读取固定身份与规则提示词，不在这里写入动态项目事实。"""
    return build_pm_system_prompt()


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
    dynamic_memory_model: bool = False,
    memory_enabled: bool = True,
) -> tuple[AgentMiddleware, ...]:
    """检查 PM 配置并装配中间件，运行生命周期交给官方接口。"""
    expected = default_pm_profile(acp_enabled=acp is not None, memory_enabled=memory_enabled)
    if profile.context_policy != expected.context_policy:
        raise ValueError("PM context_policy must use the reviewed #33 projection path")
    if profile.middleware_profile != expected.middleware_profile:
        raise ValueError(
            "PM middleware_profile must exactly match the reviewed PM requirements"
        )

    # 基础提示词先注入，再接入人工审批、项目状态和历史记忆。
    middleware = (
        make_context_projection_prompt(stable_pm_system_prompt()),
        PMModelSelectionMiddleware(),
        PMDiscussionMiddleware(acp),
        HumanDecisionRequestMiddleware(),
        human_decision_hitl_middleware(),
        project_state if project_state is not None else ProjectStateMiddleware(),
    )
    if memory_enabled:
        middleware += (PMAgentMemoryMiddleware(
            memory_jobs,
            recent_daily_days=recent_daily_days,
            context_token_threshold=compaction_token_threshold // 2,
            context_token_limit=context_token_limit,
            model_profile_enabled=dynamic_memory_model,
        ),)
    # 配置后台记忆服务后，追加压缩交接，避免长对话丢失关键依据。
    if memory_enabled and memory_jobs is not None:
        middleware += (
            PMMemorySummarizationMiddleware(
                memory_jobs,
                summary_model or memory_jobs.model,
                token_threshold=compaction_token_threshold,
                keep_messages=keep_messages,
                dynamic_model=dynamic_memory_model and summary_model is None,
                model_profile_enabled=dynamic_memory_model,
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


async def pm_agent(*, studio=False, trace_project_id=None):
    """创建 PM 主图，运行与检查点生命周期交给官方 Server。"""
    from src.middlewares.agent.pm_settings import current

    pm_settings = current()
    if pm_settings is not None:
        from src.middlewares.persistence.pm_runtime import require_ready

        require_ready()
    config = pm_settings.read() if pm_settings is not None else {}
    switches = config.get("runtime", {}).get("middleware", {})
    if not isinstance(switches, dict) or any(type(value) is not bool for value in switches.values()):
        raise ValueError("PM 中间件开关配置无效")
    memory_enabled = switches.get("memory", True)
    acp_enabled = switches.get("acp", True)
    trajectory_enabled = switches.get("trajectory", True)
    # 先确定模型与共享存储，项目状态和历史记忆使用同一个 Store。
    dynamic_memory_model = True
    store = (await get_pm_memory_store()).store
    from src.middlewares.agent.user_configurable import (
        configured_pm_model,
        pm_model_catalog,
    )

    catalog = pm_model_catalog(store)
    selected = await catalog.get_default()
    if selected is None:
        raise ValueError("尚未配置默认模型，请在设置中选择已启用的模型")
    model = await configured_pm_model(catalog, selected)
    memory = None if studio or not memory_enabled else PMMemory(store, model)
    from src.middlewares.project_state.workflow import ProjectWorkflow

    from .collaboration import reconcile_verified_jobs

    project_state = ProjectStateMiddleware(
        workflow=ProjectWorkflow(store), pm_discussion_gate=not studio,
        project_recovery=reconcile_verified_jobs if not studio else None,
    )
    trajectory = None
    workspace_item = await store.aget(("deep_loopminder", "pm_settings"), "workspace") if pm_settings is None else None
    workspace_values = config.get("workspace", {}) if pm_settings else (workspace_item.value if workspace_item else {})
    compaction_threshold, context_limit = resolve_compaction_settings(workspace_values, use_environment=pm_settings is None)
    if trace_project_id and not studio and trajectory_enabled:
        from src.middlewares.trajectory.paths import trajectory_directory

        enabled = workspace_values.get("trace_enabled", True) is True
        directory = trajectory_directory(trace_project_id) if enabled else None
    else:
        directory = (None if studio or not trajectory_enabled else
                     config.get("runtime", {}).get("trajectory_directory") if pm_settings else
                     os.getenv("PM_TRAJECTORY_DIRECTORY"))
    if directory:
        from src.middlewares.trajectory import TrajectoryMiddleware, TrajectoryRecorder

        trajectory = TrajectoryMiddleware(
            await asyncio.to_thread(TrajectoryRecorder, directory),
            stop_on_error=(config.get("runtime", {}).get("trajectory_stop_on_error", False) if pm_settings
                           else os.getenv("PM_TRAJECTORY_STOP_ON_ERROR", "false").lower() == "true"),
        )
    # 根据部署配置启用 ACP；角色与 Worker 连接仍由 ACP 中间件管理。
    specs = ([] if studio or not acp_enabled else
             list(config.get("runtime", {}).get("acp_subagents", [])) if pm_settings else
             json.loads(os.getenv("ACP_SUBAGENTS_JSON") or "[]"))
    if pm_settings is not None:
        from src.middlewares.agent.pm_settings import resolve_runtime_config

        specs = [resolve_runtime_config(spec, pm_settings) for spec in specs]
    if not studio and acp_enabled:
        from src.middlewares.acp.server import resolve_managed_subagents

        specs = await asyncio.to_thread(resolve_managed_subagents, specs)
    acp = None
    if specs:
        from src.middlewares.acp import ACPMiddleware
        from src.middlewares.acp.server import worker_catalog

        acp = ACPMiddleware(
            async_subagents=specs,
            candidate_source=worker_catalog if all(not spec.get("url") for spec in specs) else None,
            pm_governed=True,
        )
    # 最后把身份提示词、已装配的中间件和 Store 交给官方 Agent 工厂。
    return create_agent(
        model,
        system_prompt=stable_pm_system_prompt(),
        middleware=assemble_pm_user_middleware(
            default_pm_profile(acp_enabled=acp is not None, memory_enabled=memory_enabled),
            acp=acp,
            memory_jobs=memory,
            project_state=project_state,
            trajectory=trajectory,
            dynamic_memory_model=dynamic_memory_model,
            compaction_token_threshold=compaction_threshold,
            context_token_limit=context_limit,
            memory_enabled=memory_enabled,
        ),
        store=store,
    )


async def pm_agent_server():
    """Expose a zero-argument graph factory to Agent Server."""
    from langgraph.config import get_config

    configurable = get_config().get("configurable", {})
    project_id = configurable.get("project_id") if configurable.get("workbench_trace") is True else None
    return await pm_agent(trace_project_id=project_id)


async def pm_agent_studio():
    """Build a Studio-only PM with an isolated project per thread."""
    return await pm_agent(studio=True)
