"""Bridge official background runs to SDK-managed ACP sessions."""

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import NotRequired

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_store, get_stream_writer
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph_sdk import get_client

from src.middlewares.project_state.folders import ProjectFolders
from src.middlewares.project_state.persistence import ProjectStore
from src.middlewares.project_state.scope import project_write_lock
from src.middlewares.trajectory.middleware import TrajectoryRecorder
from src.runtime.routing import RoleRequirement, WorkerCandidate, route_worker

from .control import ACPWorker, UnsupportedControl
from .correlation import WorkerRef
from .pool import READONLY_ISOLATION


def project_recorder(project_id):
    """Open the project's trajectory recorder, or None when tracing is off.

    Worker notifications used to be published only to the run's own stream, so
    the workbench showed the PM side and nothing else: no worker tool calls, no
    worker messages, no worker usage. ``TrajectoryRecorder.native`` already
    normalizes exactly these notifications and the dashboard already renders the
    ``native`` kind, so the missing piece was a recorder bound to the project.
    """
    if not isinstance(project_id, str) or not project_id.strip():
        return None
    try:
        from src.middlewares.agent.pm_settings import current
        from src.middlewares.trajectory.paths import trajectory_directory

        settings = current()
        if settings is not None:
            values = settings.read()
            if (not values.get("runtime", {}).get("middleware", {}).get("trajectory", True)
                    or not values.get("workspace", {}).get("trace_enabled", True)):
                return None
        return TrajectoryRecorder(trajectory_directory(project_id))
    except (ValueError, OSError):
        # Tracing is an observation channel; losing it must not fail a run.
        return None




class ACPRunState(MessagesState):
    """Keep native identity and business correlation in the official checkpoint."""

    worker_ref: NotRequired[dict]
    owner_thread_id: NotRequired[str]
    request: NotRequired[dict]
    pm_governed: NotRequired[bool]
    mode_record: NotRequired[dict | None]


def create_worker_graph(
    workers: Mapping[str, ACPWorker],
    candidates: Sequence[WorkerCandidate],
    *,
    project_workers=None,
    governance=None,
):
    """Compile a worker bridge; execution and persistence belong to Agent Server."""
    async def prepare(state: ACPRunState, config: RunnableConfig):
        message = next(
            m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)
        )
        request = json.loads(message.content)
        # Once a session is governed, a later message must not remove its gate.
        # Read the previous request too for checkpoints created before this
        # dedicated state field existed.
        governed = bool(
            state.get("pm_governed")
            or state.get("request", {}).get("pm_governed")
            or request.get("pm_governed")
        )
        # A caller cannot strip PM governance by invoking the public bridge
        # directly with an existing project ID and omitting a boolean flag.
        claimed_project = (state.get("worker_ref") or {}).get("project_id") or request.get("project_id")
        if project_workers is not None and isinstance(claimed_project, str) and claimed_project:
            governed = governed or await ProjectStore(get_store()).load(claimed_project) is not None
        if governed:
            if governance is None:
                raise PermissionError("PM Worker 缺少协作门禁")
            request["pm_governed"] = True
            if request.get("pm_phase") not in {"discussion", "review", "execution"}:
                raise PermissionError("PM Worker 请求缺少可信的协作阶段")
            if project_workers is None:
                raise PermissionError("PM Worker 缺少项目隔离连接管理器")
            folders = ProjectFolders(get_store())
            project_folder = await folders.verified_binding(claimed_project)
            if await folders.find_overlapping_directory(project_folder["directory"], exclude_project_id=claimed_project):
                raise PermissionError("项目目录与其他项目重叠，不能启动 Worker")
            if request["pm_phase"] in {"discussion", "review"}:
                prior_ref = state.get("worker_ref") or {}
                await governance.authorize_readonly(request, claimed_project, prior_ref.get("task_id") or request.get("task_id"),
                                         prior_ref.get("thread_id") or config["configurable"]["thread_id"], config)
        if "worker_ref" in state:
            ref = WorkerRef(**state["worker_ref"])
            if request["owner_thread_id"] != state["owner_thread_id"]:
                raise PermissionError("ACP session belongs to another parent thread")
            if (
                request.get("project_id", ref.project_id) != ref.project_id
                or request.get("task_id", ref.task_id) != ref.task_id
                or request["agent_type"] != ref.role
            ):
                raise ValueError(
                    "A followup cannot change the session's project, task or role"
                )
            execution_phase = governed and request["pm_phase"] == "execution"
            if execution_phase:
                await governance.authorize_execution(request, ref, config)
            if project_workers is not None:
                binding = await ProjectFolders(get_store()).verified_binding(ref.project_id)
                if binding is None or binding["directory"] != ref.cwd:
                    raise ValueError("项目目录未绑定或与现有会话不一致")
            if execution_phase:
                # Never start a writable process in prepare: checkpoint failure
                # between graph nodes must not leave one running unattended.
                return {"worker_ref": ref.to_dict(), "owner_thread_id": request["owner_thread_id"],
                        "request": request, "pm_governed": True, "mode_record": None}
            if project_workers is not None:
                # 进程必须先于会话恢复启动：原生连接按提供方和目录持有，
                # 不能用未启动的连接对象去 load_session。
                if governed and request["pm_phase"] in {"discussion", "review"}:
                    if ref.isolation != READONLY_ISOLATION:
                        raise PermissionError("现有会话没有已声明的执行边界")
                    worker = await project_workers.get_readonly(
                        ref.provider_id, ref.cwd,
                        mode_id=await governance.readonly_mode(ref.project_id, ref.provider_id),
                        session_key=ref.thread_id,
                    )
                else:
                    worker = await project_workers.get(ref.provider_id, ref.cwd)
            else:
                worker = workers[ref.provider_id]
            await worker.resume(ref)
        else:
            if request["operation"] != "spawn":
                raise RuntimeError(
                    "Native ACP reference is missing from the official checkpoint; cannot restore this conversation"
                )
            if governed and request["pm_phase"] == "execution":
                raise PermissionError("开发必须续接已经商讨并通过门禁的负责人会话")
            decision = route_worker(
                RoleRequirement(
                    request["agent_type"],
                    frozenset(request.get("required_capabilities", ())),
                ),
                [
                    candidate
                    for candidate in candidates
                    if candidate.provider_id in workers and workers[candidate.provider_id].process.returncode is None
                ],
            )
            if decision.provider_id is None:
                raise RuntimeError(
                    decision.reason + "; rejected_reasons="
                    + json.dumps(dict(decision.rejected_reasons), ensure_ascii=False)
                )
            if governed and request.get("pm_provider_id") != decision.provider_id:
                raise PermissionError("实际 Worker 提供方与只读授权不一致")
            if project_workers is not None:
                binding = await ProjectFolders(get_store()).verified_binding(request["project_id"])
                if binding is None:
                    raise ValueError("项目尚未绑定文件夹")
                if governed and request["pm_phase"] in {"discussion", "review"}:
                    worker = await project_workers.get_readonly(
                        decision.provider_id, binding["directory"],
                        mode_id=await governance.readonly_mode(request["project_id"], decision.provider_id),
                        session_key=config["configurable"]["thread_id"],
                    )
                else:
                    worker = await project_workers.get(decision.provider_id, binding["directory"])
            else:
                worker = workers[decision.provider_id]
            ref = await worker.new(
                project_id=request["project_id"],
                task_id=request["task_id"],
                thread_id=config["configurable"]["thread_id"],
                role=request["agent_type"],
            )
        # The mode is applied and confirmed in execute, immediately before the
        # prompt is sent. Doing it here would miss Runs that restore an
        # already-created session, and the session may be shared with a turn
        # that has not been asked yet.
        mode_record = None if request.get("pm_phase") != "discussion" else state.get("mode_record")
        return {
            "worker_ref": ref.to_dict(),
            "owner_thread_id": request["owner_thread_id"],
            "request": request,
            "pm_governed": governed,
            "mode_record": mode_record,
        }


    async def execute(state: ACPRunState, config: RunnableConfig):
        ref = WorkerRef(**state["worker_ref"])
        request = state["request"]
        if request.get("pm_governed"):
            if request.get("pm_phase") == "execution":
                await governance.authorize_execution(request, ref, config)
            elif request.get("pm_phase") not in {"discussion", "review"} or ref.isolation != READONLY_ISOLATION:
                raise PermissionError("当前会话没有可信的只读边界")
            else:
                async with project_write_lock(ref.project_id):
                    await governance.authorize_readonly(request, ref.project_id, ref.task_id, ref.thread_id, config)
                    run_id = str(config.get("metadata", {}).get("run_id", ""))
                    if not run_id:
                        raise PermissionError("只读请求缺少官方 Run 标识")
                    await governance.claim_intent(ref.project_id, ref.task_id, request["pm_dispatch_id"], run_id)
        chunks = []
        usage = None
        writer = get_stream_writer()
        recorder = project_recorder(ref.project_id)
        run_id = str(config.get("metadata", {}).get("run_id", ""))

        async def notification(event):
            nonlocal usage
            update = event.update
            if update.session_update == "usage_update":
                # cost 是协议同一事件的可靠字段；适配器未上报时保持缺失，不猜。
                usage = {
                    "used": update.used, "size": update.size,
                    "remaining": update.size - update.used if update.used <= update.size else None,
                    **({"cost": update.cost.model_dump(mode="json", by_alias=True,
                                                    exclude_none=True)}
                       if getattr(update, "cost", None) is not None else {}),
                    "source": "acp.session_update.usage_update",
                    "observed_at": datetime.now(UTC).isoformat(),
                }
            if (
                update.session_update == "agent_message_chunk"
                and update.content.type == "text"
            ):
                chunks.append(update.content.text)
            # Record the native notification for the project trajectory. The
            # agent column is the provider, so the workbench separates Claude
            # and Codex instead of attributing their work to the PM.
            if recorder is not None:
                try:
                    recorder.native(ref.provider_id, event, run_id=run_id, thread_id=ref.session_id)
                except Exception:
                    # A tracing failure must never break the worker run itself.
                    pass
            # Publish the native notification as-is through the official stream.
            writer(TrajectoryRecorder.clean_public(event.model_dump(mode="json", by_alias=True, exclude_none=True)))

        execution_phase = bool(request.get("pm_governed") and request.get("pm_phase") == "execution")
        execution = None
        if execution_phase:
            ticket = project_workers.execution_ticket(ref, request["pm_dispatch_id"])
            execution = await governance.begin_execution(ref, request, config, ticket)
        trace_error = None
        try:
            worker = workers[ref.provider_id]
            if project_workers is not None:
                binding = await ProjectFolders(get_store()).verified_binding(ref.project_id)
                if binding is None or binding["directory"] != ref.cwd:
                    raise ValueError("项目目录未绑定或与现有会话不一致")
                if request.get("pm_governed") and request.get("pm_phase") in {"discussion", "review"}:
                    worker = await project_workers.get_readonly(
                        ref.provider_id, ref.cwd,
                        mode_id=await governance.readonly_mode(ref.project_id, ref.provider_id),
                        session_key=ref.thread_id,
                    )
                    if worker.readonly_enforcement is None:
                        raise PermissionError("只读执行能力不可验证")
                    await worker.resume(ref)
                elif execution_phase:
                    worker = await project_workers.get_execution(
                        ref, request["pm_dispatch_id"], job_status=execution["job_status"]
                    )
                    await worker.resume(ref)
                    await governance.apply_execution_mode(worker, ref)
                    await governance.authorize_execution(request, ref, config)
                else:
                    worker = await project_workers.get(ref.provider_id, ref.cwd)
            if request.get("pm_governed") and request.get("pm_phase") in {"discussion", "review"}:
                # Recheck after connection setup as close as possible to ask.
                # This is not an atomic send against unrelated filesystem or
                # Store writers; stale results must still fail subsequent checks.
                await governance.authorize_readonly(request, ref.project_id, ref.task_id, ref.thread_id, config)
            # The mode is set and confirmed here, immediately before the prompt
            # leaves the host. A mismatch raises and no discussion content is
            # ever processed by the adapter under the wrong mode.
            mode_record = await governance.apply_discussion_mode(worker, ref, request, config) if governance is not None else None
            # Bracket the ask so the workbench can group this worker's native
            # notifications into one turn instead of a flat stream.
            if recorder is not None:
                try:
                    recorder.emit(
                        "turn", "start", "负责人轮次开始",
                        agent=ref.provider_id, thread_id=ref.session_id, run_id=run_id,
                        data={"task_id": ref.task_id, "phase": request.get("pm_phase")},
                    )
                except Exception:
                    pass
            result = await worker.control(
                ref,
                "ask",
                {"text": state["request"]["message"]},
                on_update=notification,
            )
        except Exception as exc:
            trace_error = exc
            raise
        finally:
            if recorder is not None:
                try:
                    recorder.emit(
                        "turn", "error" if trace_error is not None else "end",
                        "负责人轮次结束" if trace_error is None else "负责人轮次失败",
                        agent=ref.provider_id, thread_id=ref.session_id, run_id=run_id,
                        data={"task_id": ref.task_id,
                              "error": str(trace_error)[:500] if trace_error is not None else None},
                    )
                except Exception:
                    pass
            if execution_phase:
                try:
                    await asyncio.shield(project_workers.release_execution(ref))
                except Exception as exc:
                    await governance.finish_execution(ref, execution, exc)
                else:
                    await governance.finish_execution(ref, execution)
        return {
            "messages": [
                AIMessage(
                    content=TrajectoryRecorder.clean_for_judgment("".join(chunks)),
                    additional_kwargs={
                        "acp_stop_reason": result.stop_reason,
                        "acp_run_id": str(config.get("metadata", {}).get("run_id", "")),
                        "context_usage": usage,
                        "compaction_visibility": "unknown",
                    },
                )
            ],
            "mode_record": mode_record,
        }

    return (
        StateGraph(ACPRunState)
        .add_node("prepare", prepare)
        .add_node("execute", execute)
        .add_edge(START, "prepare")
        .add_edge("prepare", "execute")
        .add_edge("execute", END)
        .compile()
    )


class ACPControlState(MessagesState):
    """Input for a one-shot control run on the same official server."""

    target: str
    owner_thread_id: str
    message: str
    pm_governed: NotRequired[bool]


def create_control_graph(workers: Mapping[str, ACPWorker], *, project_workers=None):
    """Expose steer through an official graph, including for HTTP deployments."""

    async def steer(state: ACPControlState):
        client = get_client(url=None)
        snapshot = await client.threads.get_state(state["target"])
        values = snapshot["values"]
        if not values.get("worker_ref"):
            return {
                "messages": [AIMessage(content=json.dumps({"outcome": "not_ready", "next_action": "wait_agent"}))]
            }
        if values.get("owner_thread_id") != state["owner_thread_id"]:
            raise PermissionError("ACP target belongs to another parent thread")
        ref = WorkerRef(**values["worker_ref"])
        if (state.get("pm_governed") or values.get("pm_governed")
                or values.get("request", {}).get("pm_governed")):
            return {"messages": [AIMessage(content=json.dumps({
                "outcome": "unsupported", "next_action": "wait_agent",
                "reason": "steering_requires_versioned_authorization",
            }))]}
        worker = workers.get(ref.provider_id)
        if project_workers is not None and worker is not None:
            binding = await ProjectFolders(get_store()).get(ref.project_id)
            if binding is None or binding["directory"] != ref.cwd:
                raise ValueError("项目目录未绑定或与现有会话不一致")
            worker = await project_workers.get(ref.provider_id, ref.cwd)
        if worker is None or worker.process.returncode is not None:
            result = {"outcome": "worker_unavailable", "next_action": "list_agents"}
        else:
            try:
                await worker.resume(ref)
                result = await worker.control(ref, "steer", {"text": state["message"]})
                if result.get("outcome") != "injected":
                    result["next_action"] = "followup_task"
            except UnsupportedControl:
                result = {"outcome": "unsupported", "next_action": "followup_task"}
            except PermissionError:
                result = {"outcome": "permission_denied", "next_action": "review_worker_grants"}
        return {"messages": [AIMessage(content=json.dumps(result, ensure_ascii=False))]}

    return (
        StateGraph(ACPControlState)
        .add_node("steer", steer)
        .add_edge(START, "steer")
        .add_edge("steer", END)
        .compile()
    )
