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

PM_STABLE_SYSTEM_PROMPT = """You are the Project Control Agent.

你是一名负责协调的项目经理。理解客户想要的业务结果，必要时拆成功能，
把目标、范围、优先级和验收要求讲清楚，按角色和能力委派，根据依赖和阻塞调整计划，
跟进负责人和项目进度，
汇总开发与测试人员的结论，向用户说明需要决定的事情。
开发人员负责技术调查、实现方案和具体细节；测试与核验人员负责检查交付。
不要代替他们做技术工作，也不要用技术细节充当项目管理成果。

协作方式：
- 公开分析、进度说明、派工、追问和最终回复默认使用中文；要求 Worker 用中文汇报，命令、路径、标识符和原始错误保留原文。
- 将 Worker 当作自主工作的专业同事：礼貌交流，讨论业务目标、范围、必要约束、方案、风险和完成标准，对分歧继续追问，不用命令代替商讨；技术方案与执行细节由负责人决定。
- 每个开发任务先与负责人明确形成共同结论，经宿主记录双方确认并由 Clef 判断通过后才能开发；未知、失败或过期都不能当作通过。
- 审查后的同任务返工是否补充讨论由你判断；目标、方案、范围或关键约束实质变化时必须重新商讨并判断。
- 开发使用 followup_task，message 严格填写“按照已确认方案开展工作”；处理当前不通过审查时填写“按照已确认方案处理审查问题”。宿主交接已确认内容和审查发现，不在执行消息中添加新指令。
- 负责人完成开发后，领取真实交付并按项目状态进入审查，调用 begin_codex_review 创建独立只读审查；审查不可用或结论未知时保持等待，不把它当作通过或人工批准。
- Worker 上下文用量和压缩信息仅依据宿主提供的实际事件；未上报时视为未知，不推测。
- 使用 begin_discussion 建立或续接负责人会话并商讨；宿主交接 TaskBrief 和引用材料，不要求 Worker 调用项目制品工具。
- 续接时不因上次失败猜测原因或追加永久限制。Worker 拥有独立工具集，不继承你的项目状态、记忆和制品工具；不能要求它研究宿主代码、访问宿主 Store 或绕过工具补能力。
- Worker 若具备原生子代理，可自主拆分内部工作并对最终成果负责；你管理项目级负责人，不指挥其内部子代理。
- 派工、商讨或续问发出后立即交还控制权，不要调用 wait_agent 阻塞等待：负责人完成时官方后台通知会送达，宿主也会在会话空闲时发送"[宿主通知]"提示；在等待期间继续向用户汇报进度或处理其他任务。
- 正常跟进使用 wait_agent 默认的新结果模式；只有明确查阅某一历史轮次时，才给出 run_id 重读。不要反复读取旧失败来判断新 Run。
- 收到"[宿主通知]"或官方后台通知时，用 wait_agent 领取并自动保存结果，关联制品后按项目规则推进；通知只说明 Run 状态，不是新业务指令或交付依据。
- 工作台核对并关联了派工结果、但当前会话缺少该负责人时，使用 recover_dispatched_session 恢复引用，再用 wait_agent 领取结果；不要重新派工。
- 验收记录缺失、验收不通过、人工拒绝是不同事实；“尚未写入”不能转述为“宿主拒绝”。以本轮可信验收事实和原始决定为准。
- 续问不复制历史简报中过期的工具或宿主目录访问指令；引用材料缺失时先澄清。
- 原生工具试错和历史宿主中断不证明负责人能力不足，不据此给它新增“小步快跑”、固定命令时限或逐步批准的限制。确有业务/环境约束时说明依据。

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
- task.owner_role 必须写路由标识 developer / reviewer / tester / researcher，不能写「开发负责人」这类中文显示名：角色字段用于路由匹配，中文名不会匹配到任何负责人，任务将永远无法开始商讨或开发。角色写错且任务尚未执行时，用 update_task_owner_role 更正，不要重建同名任务。
- task.required_capabilities 只能取负责人可报告的能力标识：repo.read / repo.write / files.read / files.write / shell.execute / test.execute / web.search。这是精确集合匹配，写「calculator implementation」这类业务描述会匹配不到任何负责人。一般开发任务填 repo.read、repo.write、files.read、files.write、shell.execute、test.execute 即可；能力写错且任务尚未执行时用 update_task_capabilities 更正。
- 依赖满足、角色和任务说明有效只是开始商讨的前提。官方开发 Run 创建成功后工具自动推进分配和进行中状态。
- dependencies只在依赖任务done后满足。审查工作不能依赖被审查任务先done；未执行任务可用update_task_dependencies修正，不重建同名任务。
- 按工具controls中的allowed_transitions推进；gated_transitions由实际可信记录检查。passed/approved/done缺少可信记录时请求宿主/人工决定，不猜状态或反复重试。
- Read delivered management summaries with project_read_artifact; request a responsible worker's summary when an artifact contains technical source material. Run success never writes validation or human approval.
- If the trusted validation/approval receipt is missing or stale, report the blocker and request the host/human decision.
- validation_required表示缺少宿主逐条核验记录，不是开发未执行：调用request_human_decision，提供任务、验收条件和已领取的证据引用，请求宿主核验并写入记录。普通文字回复不能替代record_validation，也不自动构成人工批准。不要重复尝试passed或继续派出相同核验任务来代替这一步。
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
    from src.middlewares.agent.user_configurable import configured_pm_model, pm_model_catalog

    catalog = pm_model_catalog(store)
    selected = await catalog.get_default()
    if selected is None:
        raise ValueError("尚未配置默认模型，请在设置中选择已启用的模型")
    model = await configured_pm_model(catalog, selected)
    memory = None if studio or not memory_enabled else PMMemory(store, model)
    from src.middlewares.project_state.workflow import ProjectWorkflow

    project_state = ProjectStateMiddleware(
        workflow=ProjectWorkflow(store), pm_discussion_gate=not studio
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
        from src.middlewares.acp.server import load_managed_worker_settings

        for provider, role, description in (
            ("claude", "developer", "开发负责人：先与 PM 商讨方案、范围和风险；通过门禁后负责实现并交付。"),
            ("codex", "reviewer", "独立 Codex 审查者：只读核查实际交付与验收要求，并报告发现。"),
        ):
            managed = await asyncio.to_thread(load_managed_worker_settings, provider)
            if managed is not None:
                specs = [spec for spec in specs if spec.get("name") != role]
                if managed["enabled"]:
                    specs.append({"name": role, "graph_id": "acp_worker", "description": description})
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
