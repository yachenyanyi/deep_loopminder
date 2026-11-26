"""项目状态工具、串行写入与 PM 双轨派工门禁。"""

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import ToolException
from langgraph.types import Command

from .artifacts import content_hash
from .persistence import ProjectStore
from .scope import STUDIO_GRAPH_ID, project_write_lock, resolve_project_id
from .tools import (
    FactsProvider,
    make_project_command,
    make_project_read,
    project_scope,
    require_writer,
    requires_discussion,
)
from .transitions import ProjectStateError, task_controls

if TYPE_CHECKING:
    from .workflow import ProjectWorkflow


_WORKER_BOUNDARY = """当前宿主协作边界（优先于历史简报中冲突的工具说明）：
默认中文汇报，技术方案和内部步骤由你决定；可使用执行端实际提供的原生子代理。
你不继承PM的project_create_artifact/project_read_artifact、状态或记忆工具。
宿主已在本消息交接可用材料，成果直接通过会话返回，由PM领取保存。
若简报要求调用上述未提供的工具或查找宿主仓库、接口、Store，请报告该部分不可执行并继续可做的工作；不要照旧指令猜路径或搜索宿主。
原生工具失败由你自行判断并恢复。
"""


class ProjectStateMiddleware(AgentMiddleware):
    """为 Agent 注入当前项目状态和操作工具，写入需由宿主显式启用。

    每次调用都读取官方 Store，不在中间件实例中保存会话状态。
    facts_provider 由宿主提供可信验收与审批结果，不暴露给模型。
    """

    def __init__(
        self,
        *,
        facts_provider: FactsProvider | None = None,
        workflow: "ProjectWorkflow | None" = None,
        pm_discussion_gate: bool = False,
        project_recovery=None,
    ):
        """注册项目工具，绑定可选的可信验收与审批查询函数。"""
        self.workflow = workflow
        self.pm_discussion_gate = pm_discussion_gate
        self.project_recovery = project_recovery
        self.tools = [
            make_project_read(workflow, pm_discussion_gate=pm_discussion_gate),
            make_project_command(
                facts_provider or (workflow.facts if workflow else None),
                artifacts=workflow.artifacts if workflow else None,
            ),
        ]
        if workflow is None:
            return

        @tool
        async def project_create_artifact(
            task_id: str,
            content: str,
            runtime: ToolRuntime,
            kind: Literal["task_brief", "note"] = "task_brief",
        ) -> dict:
            """创建不可变项目制品，返回 reference 供 task_brief_ref 使用，禁止编造引用。可在建任务前创建任务简报。独立制品可并行创建，但不可与 project_command、spawn_agent、begin_discussion、followup_task、wait_agent 混批，必须先读取返回引用。"""
            require_writer(runtime)
            project_id, _ = project_scope(runtime)
            if await ProjectStore(runtime.store).load(project_id) is None:
                raise ValueError("请先创建项目，再创建项目制品")
            value = await workflow.artifacts.create(project_id, task_id, kind, content)
            return {
                key: value[key]
                for key in ("reference", "task_id", "kind", "content_hash")
            }

        @tool
        async def project_read_artifact(
            reference: str, runtime: ToolRuntime, offset: int = 0, limit: int = 6000
        ) -> dict:
            """按 reference 分页读取项目制品，不读取项目源码文件。offset 与 limit 按字符计数，limit 为 1..20000。"""
            project_id, _ = project_scope(runtime)
            if runtime.store is not workflow.store:
                raise ValueError("项目运行与制品服务必须使用同一Store")
            if offset < 0 or not 1 <= limit <= 20000:
                raise ToolException("offset>=0，limit必须在1到20000之间")
            try:
                value = await workflow.artifacts.get(project_id, reference)
            except ValueError as exc:
                raise ToolException(str(exc)) from exc
            return {
                **value,
                "content": value["content"][offset : offset + limit],
                "offset": offset,
                "total_chars": len(value["content"]),
            }

        project_read_artifact.handle_tool_error = True
        self.tools.extend((project_create_artifact, project_read_artifact))

    async def awrap_model_call(self, request, handler):
        """项目事实由工具读取，模型请求保持原样。"""
        return await handler(request)

    async def _requires_discussion(self, project_id, task, configured) -> bool:
        """会话开关或任务商讨记录要求 Clef 门禁，查询不修改状态。"""
        return await requires_discussion(self.workflow, project_id, task, configured)

    async def _worker_materials(self, project_id, brief):
        """将简报直接引用的材料交给执行者；不要求它拥有PM工具或查宿主接口。"""
        return await self.workflow.artifacts.worker_materials(project_id, brief)

    async def awrap_tool_call(self, request, handler):
        """通过本机项目锁串行提交写操作。"""
        # The remote Run also needs this lock to begin and to clear its
        # quarantine. Never hold it while wait_agent joins that same Run.
        if request.tool_call["name"] == "wait_agent":
            return await self._execute_tool_call(request, handler)
        if request.tool_call["name"] in {
            "project_command", "project_create_artifact", "spawn_agent",
            "followup_task", "wait_agent", "send_message", "assess_discussion", "begin_discussion", "begin_review", "recover_dispatched_session",
        }:
            try:
                project_id = resolve_project_id(request.runtime.config)
            except ValueError:
                return await self._execute_tool_call(request, handler)
            async with project_write_lock(project_id):
                if self.project_recovery is not None and self.workflow is not None:
                    await self.project_recovery(self.workflow.store, project_id, locked=True)
                return await self._execute_tool_call(request, handler)
        return await self._execute_tool_call(request, handler)

    async def _execute_tool_call(self, request, handler):
        """允许独立制品并行，拒绝多次快照写或混合写，避免快照覆盖与依赖竞态。"""
        name = request.tool_call["name"]
        configured = request.runtime.config.get("configurable", {})
        claim_discussion_task = None
        if self.pm_discussion_gate and configured.get("_pm_claim_event") is True:
            successful_runs = configured.get("_pm_claim_successful_runs") or ()
            project_id = configured.get("project_id")
            if self.workflow and isinstance(project_id, str):
                for run_id in successful_runs:
                    binding = await self.workflow.store.aget(
                        ("deep_loopminder", "project_execution_refs", project_id), run_id
                    )
                    value = binding.value if binding else {}
                    if (value.get("phase") == "discussion"
                            and value.get("owner_thread") == configured.get("thread_id")
                            and isinstance(value.get("task_id"), str)):
                        claim_discussion_task = value["task_id"]
                        break
        claim_task = request.tool_call.get("args", {}).get("task_id")
        claim_followup = name == "followup_task" and claim_discussion_task is not None
        claim_discussion = (claim_discussion_task is not None
                            and name == "begin_discussion" and claim_task == claim_discussion_task)
        if (self.pm_discussion_gate and configured.get("_pm_claim_event") is True
                and ((name in {"spawn_agent", "followup_task", "begin_discussion"}
                      and not (claim_followup or claim_discussion)) or (
                    name == "begin_review" and not (
                        configured.get("_pm_claim_successful_runs") and request.tool_call["args"].get("task_id")
                    )
                ))):
            return ToolMessage(
                content=json.dumps({"ok": False, "error": {
                    "code": "claim_event_dispatch_blocked",
                    "message": "结果领取通知只允许按需审查本轮领取的当前开发交付，其他派工请在普通对话中处理。",
                }}, ensure_ascii=False),
                tool_call_id=request.tool_call["id"], name=name, status="error",
                additional_kwargs={"recoverable_business_rejection": True},
            )
        if name == "send_message" and self.pm_discussion_gate and self.workflow:
            target = request.tool_call.get("args", {}).get("target")
            for key, tracked in request.runtime.state.get("async_tasks", {}).items():
                if target not in {key, request.runtime.state.get("acp_task_names", {}).get(key)}:
                    continue
                binding = await self.workflow.store.aget(
                    ("deep_loopminder", "project_execution_refs", configured["project_id"]), tracked["run_id"]
                )
                blocked = bool(binding and binding.value.get("phase") in {"discussion", "review"})
                if binding and not blocked:
                    _, task = await self.workflow.task(configured["project_id"], binding.value["task_id"])
                    if await self._requires_discussion(configured["project_id"], task, configured):
                        from src.deep_agents.agents.pm_agent.discussion import (
                            DiscussionGate,
                        )

                        try:
                            await DiscussionGate(self.workflow.store).require_pass(
                                configured["project_id"], task.task_id, worker_agent_id=key
                            )
                        except ValueError:
                            blocked = True
                if blocked:
                    return ToolMessage(
                        content=json.dumps({"ok": False, "error": {
                            "code": "steering_requires_versioned_authorization",
                            "message": "商讨或审查阶段不能插话，请等待结果后继续。", "next_action": "wait_agent",
                        }}, ensure_ascii=False), tool_call_id=request.tool_call["id"], name=name,
                        status="error", additional_kwargs={"recoverable_business_rejection": True},
                    )
        writes = {"project_command", "project_create_artifact"}
        if self.workflow:
            writes |= {"spawn_agent", "followup_task", "wait_agent", "assess_discussion",
                       "begin_discussion", "begin_review", "recover_dispatched_session"}
        if name not in writes and name != "project_read":
            return await handler(request)
        dispatched = False
        latest = next(
            (
                m
                for m in reversed(request.state["messages"])
                if isinstance(m, AIMessage)
            ),
            None,
        )
        calls = (
            [call for call in latest.tool_calls if call["name"] in writes]
            if latest
            else []
        )
        # 制品按内容哈希独立保存，不读改写项目快照；纯制品批次无需串行。
        # 混入状态修改/派工/领取结果时整批拒绝，不能只拒绝其中一部分。
        artifact_batch = all(
            call["name"] == "project_create_artifact" for call in calls
        )
        spawn_batch = (
            all(call["name"] == "spawn_agent" for call in calls)
            and len({call["args"].get("task_name") for call in calls}) == len(calls)
            and len(
                {
                    call["args"].get("task_id") or call["args"].get("task_name")
                    for call in calls
                }
            )
            == len(calls)
        )
        if name in writes and len(calls) > 1 and not (artifact_batch or spawn_batch):
            return ToolMessage(
                content=json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "parallel_project_writes",
                            "message": "独立制品可并行创建，不同业务任务可同批派工（宿主串行提交）；其他项目修改和结果领取逐次执行，不可混批。",
                            "next_action": "retry_mutations_sequentially",
                            "rejected_tools": [call["name"] for call in calls],
                        },
                    },
                    ensure_ascii=False,
                ),
                tool_call_id=request.tool_call["id"],
                name=name,
                additional_kwargs={"recoverable_business_rejection": True},
                status="error",
            )
        try:
            task_id = None
            intent = None
            phase = "execution"
            runtime = request.runtime
            args = request.tool_call["args"]
            if self.workflow:
                project_id, _ = project_scope(runtime)
                if runtime.store is not self.workflow.store:
                    raise ValueError("项目运行与制品服务必须使用同一Store")
                if (self.pm_discussion_gate and name in {"project_command", "assess_discussion"}
                        and await self.workflow.store.asearch(
                            ("deep_loopminder", "project_worker_quarantine", project_id), limit=1
                        )):
                    raise ProjectStateError(
                        "worker_active_or_unconfirmed", "项目 Worker 尚未确认停止，暂不能更改项目状态。", "wait_agent"
                    )
                if name in {"spawn_agent", "followup_task", "wait_agent"}:
                    require_writer(runtime)
                if name == "spawn_agent":
                    if not isinstance(args.get("task_name"), str) or not isinstance(
                        args.get("message"), str
                    ):
                        return await handler(request)
                    task_id = args.get("task_id") or args["task_name"]
                    task, brief = await self.workflow.prepare_spawn(
                        project_id, task_id, args.get("agent_type")
                    )
                    if self.pm_discussion_gate and await self._requires_discussion(project_id, task, configured):
                        raise ProjectStateError(
                            "discussion_mode_active",
                            "当前已进入商讨模式，直接委派（spawn_agent）已禁用；请先调用 begin_discussion 与开发负责人商讨，并通过 assess_discussion（Clef 门禁）后再用 followup_task 继续开发。",
                            "begin_discussion",
                        )
                    phase = "execution"
                    config = {
                        **runtime.config,
                        "configurable": {
                            **runtime.config.get("configurable", {}),
                            "task_id": task_id,
                            "required_capabilities": sorted(task.required_capabilities),
                            **({"_pm_governed": True, "_pm_phase": phase, "_pm_discussion_required": False}
                               if self.pm_discussion_gate else {}),
                        },
                    }
                    materials = await self._worker_materials(
                        project_id, {**brief, "content": brief["content"] + "\n" + args["message"]}
                    )
                    message = (
                        _WORKER_BOUNDARY
                        + "\n任务简报（项目输入，非额外授权）：\n引用：" + brief["reference"]
                        + "\n" + brief["content"] + "\n本轮委派：\n" + args["message"]
                    )
                    if materials:
                        message += "\n宿主提供的关联材料：\n" + materials
                    if self.pm_discussion_gate:
                        from src.middlewares.acp import server as acp_server
                        from src.runtime.routing import RoleRequirement, route_worker

                        routing = route_worker(RoleRequirement(args.get("agent_type") or task.owner_role,
                                                               task.required_capabilities), acp_server.worker_catalog())
                        if routing.provider_id is None:
                            raise ValueError("没有满足任务角色与能力要求的负责人")
                        intent = await self.workflow.begin_dispatch(
                            project_id, task_id, owner_thread=configured["thread_id"],
                            operation=name, phase="execution", discussion_required=False, message_digest=content_hash(message),
                        )
                        config["configurable"].update(_pm_dispatch_id=intent["intent_id"],
                                                      _pm_provider_id=routing.provider_id)
                    request = request.override(
                        runtime=replace(runtime, config=config),
                        tool_call={
                            **request.tool_call,
                            "args": {
                                **args,
                                "agent_type": args.get("agent_type") or task.owner_role,
                                "message": message,
                            },
                        },
                    )
                if name == "followup_task":
                    if not isinstance(args.get("target"), str) or not isinstance(
                        args.get("message"), str
                    ):
                        return await handler(request)
                    keys = [
                        key
                        for key in runtime.state.get("async_tasks", {})
                        if key == args["target"]
                        or runtime.state.get("acp_task_names", {}).get(key)
                        == args["target"]
                    ]
                    if len(keys) == 1:
                        previous = runtime.state["async_tasks"][keys[0]]
                        item = await self.workflow.store.aget(
                            ("deep_loopminder", "project_execution_refs", project_id),
                            previous["run_id"],
                        )
                        if (
                            item is None
                            or item.value["owner_thread"]
                            != runtime.config["configurable"]["thread_id"]
                        ):
                            raise ValueError("续问没有有效的业务任务关联")
                        task_id = item.value["task_id"]
                        task, brief = await self.workflow.prepare_spawn(
                            project_id, task_id, previous["agent_name"]
                        )
                        gated = self.pm_discussion_gate and await self._requires_discussion(project_id, task, configured)
                        phase = "execution"
                        decision = None
                        if gated:
                            from src.deep_agents.agents.pm_agent.discussion import (
                                DiscussionGate,
                            )

                            try:
                                decision = await DiscussionGate(self.workflow.store).require_pass(
                                    project_id, task_id, worker_agent_id=keys[0]
                                )
                            except ValueError as exc:
                                raise ProjectStateError(
                                    "discussion_required",
                                    "当前任务缺少与负责人和输入版本匹配的商讨通过记录。",
                                    "begin_discussion",
                                ) from exc
                        if not task.required_capabilities <= set(
                            item.value["required_capabilities"]
                        ):
                            raise ValueError("任务能力要求已增加，请重新派工")
                        # 原生会话保留历史；同版任务不重复灌入简报，更新输入时才补齐。
                        snapshot, _ = await self.workflow.task(project_id, task_id)
                        changed = item.value[
                            "input_version"
                        ] != await self.workflow.input_version(snapshot, task)
                        message = args["message"]
                        refresh_brief = changed or not item.value.get(
                            "worker_context_version"
                        )
                        if refresh_brief:
                            message = (
                                "任务要求已更新：\n"
                                + brief["content"]
                                + "\n本轮补充：\n"
                                + message
                            )
                        # 续问新增引用也由宿主展开，不能要求Worker自己调用PM读取工具。
                        materials = await self._worker_materials(
                            project_id,
                            {
                                **brief,
                                "content": (brief["content"] if refresh_brief else "")
                                + "\n"
                                + args["message"],
                            },
                        )
                        if materials:
                            message += "\n本轮新增关联材料：\n" + materials
                        message += "\n" + _WORKER_BOUNDARY
                        if gated:
                            message = ("请依据本会话中已确认的方案和以下任务范围开展工作；"
                                       "不得扩大目标、方案、范围或关键约束，如需改变请先暂停并与 PM 商讨。\n"
                                       "项目目标：\n" + snapshot.goal
                                       + "\n项目约束：\n" + json.dumps(list(snapshot.constraints), ensure_ascii=False)
                                       + "\n验收标准：\n" + json.dumps(list(task.acceptance_criteria), ensure_ascii=False)
                                       + "\n任务说明：\n" + brief["content"] + "\n已确认结论：\n"
                                       + decision["pm_confirmation"] + "\n本轮开发或返工说明：\n" + args["message"])
                            approved_materials = await self._worker_materials(project_id, brief)
                            if approved_materials:
                                message += "\n任务说明引用的材料：\n" + approved_materials
                            review = await self.workflow.independent_review_status(project_id, task_id)
                            if review.get("status") == "fail":
                                message += ("\n当前独立审查发现：\n" + json.dumps(
                                    {"summary": review.get("summary"), "findings": review.get("findings")}, ensure_ascii=False))
                            message += "\n" + _WORKER_BOUNDARY
                        request = request.override(
                            runtime=replace(runtime, config={
                                **runtime.config,
                                "configurable": {
                                    **runtime.config.get("configurable", {}),
                                    **({"_pm_governed": True, "_pm_phase": phase, "_pm_discussion_required": gated}
                                       if self.pm_discussion_gate else {}),
                                },
                            }),
                            tool_call={
                                **request.tool_call,
                                "args": {
                                    **args,
                                    "message": message,
                                },
                            }
                        )
            if self.workflow and self.pm_discussion_gate and name == "followup_task":
                if not task_id:
                    raise ValueError("续问没有有效的业务任务关联")
                intent = await self.workflow.begin_dispatch(
                    project_id, task_id, owner_thread=runtime.config["configurable"]["thread_id"],
                    operation=name, phase=phase, discussion_required=gated,
                    decision_id=decision["decision_id"] if decision else None, worker_agent_id=keys[0],
                    message_digest=content_hash(request.tool_call["args"]["message"]),
                )
                dispatch_runtime = request.runtime
                request = request.override(runtime=replace(
                    dispatch_runtime,
                    config={**dispatch_runtime.config, "configurable": {
                        **dispatch_runtime.config.get("configurable", {}),
                        "_pm_dispatch_id": intent["intent_id"],
                    }},
                ))
            result = await handler(request)
            dispatched = True
            if (
                self.workflow
                and isinstance(result, Command)
                and isinstance(result.update, dict)
            ):
                owner = runtime.config["configurable"]["thread_id"]
                if task_id and name in {"spawn_agent", "followup_task"}:
                    for agent_id, tracked in result.update.get(
                        "async_tasks", {}
                    ).items():
                        await self.workflow.record_start(
                            project_id, task_id, owner, agent_id, tracked["run_id"],
                            phase=phase, dispatch_id=intent["intent_id"] if intent else None,
                        )
                        if intent:
                            await self.workflow.complete_dispatch(
                                project_id, task_id, agent_id=agent_id, run_id=tracked["run_id"]
                            )
                if name == "wait_agent":
                    async with project_write_lock(project_id):
                        for index, message in enumerate(result.update.get("messages", [])):
                            if (
                                isinstance(message, ToolMessage)
                                and message.tool_call_id == request.tool_call["id"]
                            ):
                                payload = json.loads(message.content)
                                for completed in payload.get("completed", []):
                                    completed["artifact_ref"] = await self.workflow.record_result(
                                        project_id, owner, completed
                                    )
                                    source = await self.workflow.store.aget(
                                        ("deep_loopminder", "project_execution_refs", project_id),
                                        completed["run_id"],
                                    )
                                    if source and source.value.get("phase") == "review":
                                        try:
                                            review = await self.workflow.record_independent_review(
                                                project_id, source.value["task_id"], review_run_id=completed["run_id"]
                                            )
                                            completed["independent_review"] = {
                                                "decision": review["decision"], "review_id": review["review_id"]
                                            }
                                        except ValueError as exc:
                                            completed["independent_review"] = {"decision": "unknown", "reason": str(exc)}
                                result.update["messages"][index] = message.model_copy(
                                    update={"content": json.dumps(payload, ensure_ascii=False)}
                                )
            if (
                self.workflow
                and name == "project_command"
                and args.get("target_status") in ("failed", "rework")
                and isinstance(result, ToolMessage)
                and result.status != "error"
            ):
                await self.workflow.invalidate(project_id, args["task_id"])
            return result
        except ValueError as exc:
            # 外部Run已创建后的关联/落盘错误不能伪装成可安全重试的参数错误。
            if dispatched and name in {"spawn_agent", "followup_task", "wait_agent"}:
                raise RuntimeError(
                    "外部调用已返回，但业务来源记录失败；停止并核对官方Run，禁止重复派工"
                ) from exc
            controls = None
            args = request.tool_call["args"]
            identity = args.get("task_id") or (args.get("task") or {}).get("task_id")
            if name == "spawn_agent":
                identity = identity or args.get("task_name")
            snapshot = None
            if request.runtime.store is not None and request.runtime.config.get(
                "configurable", {}
            ).get("project_id") or (
                request.runtime.store is not None
                and request.runtime.config.get("configurable", {}).get("graph_id")
                == STUDIO_GRAPH_ID
            ):
                project_id, store = project_scope(request.runtime)
                snapshot = await store.load(project_id)
            if snapshot and any(task.task_id == identity for task in snapshot.tasks):
                controls = task_controls(snapshot, identity)
            return ToolMessage(
                content=json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "project_command_rejected",
                            "message": str(exc),
                            "next_action": "inspect_controls_or_request_host",
                            "controls": controls,
                            **getattr(exc, "details", {}),
                        },
                    },
                    ensure_ascii=False,
                ),
                tool_call_id=request.tool_call["id"],
                name=name,
                # 仅宿主类型化业务拒绝可恢复；普通ValueError与落盘/ACP故障不豁免。
                additional_kwargs={
                    "recoverable_business_rejection": isinstance(exc, ProjectStateError)
                },
                status="error",
            )
