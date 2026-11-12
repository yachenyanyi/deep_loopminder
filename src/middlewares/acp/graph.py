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
from src.middlewares.project_state.artifacts import content_hash
from src.middlewares.project_state.persistence import ProjectStore
from src.middlewares.project_state.scope import project_write_lock
from src.middlewares.trajectory.middleware import TrajectoryRecorder
from src.runtime.routing import RoleRequirement, WorkerCandidate, route_worker

from .control import ACPWorker, UnsupportedControl
from .correlation import WorkerRef


class ACPRunState(MessagesState):
    """Keep native identity and business correlation in the official checkpoint."""

    worker_ref: NotRequired[dict]
    owner_thread_id: NotRequired[str]
    request: NotRequired[dict]
    pm_governed: NotRequired[bool]


def create_worker_graph(
    workers: Mapping[str, ACPWorker],
    candidates: Sequence[WorkerCandidate],
    *,
    project_workers=None,
):
    """Compile a worker bridge; execution and persistence belong to Agent Server."""

    async def authorize_execution(request, ref, config):
        from src.deep_agents.agents.pm_agent.discussion import DiscussionGate

        decision = await DiscussionGate(get_store()).require_pass(
            ref.project_id, ref.task_id, worker_agent_id=ref.thread_id
        )
        intent_id = request.get("pm_dispatch_id")
        if not isinstance(intent_id, str) or not intent_id:
            raise PermissionError("开发请求缺少持久化派工意图")
        intent = await get_store().aget(
            ("deep_loopminder", "project_dispatch_intents", ref.project_id, "history"), intent_id
        )
        value = intent.value if intent else {}
        run_id = str(config.get("metadata", {}).get("run_id", ""))
        if (value.get("intent_id") != intent_id or value.get("phase") != "execution"
                or value.get("project_id") != ref.project_id or value.get("task_id") != ref.task_id
                or value.get("worker_agent_id") != ref.thread_id
                or value.get("owner_thread") != request.get("owner_thread_id")
                or value.get("decision_id") != decision.get("decision_id")
                or value.get("input_version") != decision.get("input_version")
                or not isinstance(request.get("message"), str)
                or value.get("message_digest") != content_hash(request["message"])
                or value.get("status") not in {"pending", "confirmed"}
                or (value.get("claimed_run_id") and value.get("claimed_run_id") != run_id)
                or (value.get("status") == "confirmed" and value.get("run_id") != run_id)):
            raise PermissionError("派工意图与当前负责人或门禁版本不匹配")

    async def authorize_readonly(request, project_id, task_id, worker_id, config):
        from src.middlewares.project_state.workflow import ProjectWorkflow

        intent_id = request.get("pm_dispatch_id")
        if not isinstance(intent_id, str) or not intent_id:
            raise PermissionError("只读请求缺少持久化意图")
        item = await get_store().aget(("deep_loopminder", "project_dispatch_intents", project_id, "history"), intent_id)
        value = item.value if item else {}
        run_id = str(config.get("metadata", {}).get("run_id", ""))
        workflow = ProjectWorkflow(get_store())
        snapshot, task = await workflow.task(project_id, task_id)
        if (value.get("intent_id") != intent_id or value.get("phase") != request.get("pm_phase")
                or value.get("phase") not in {"discussion", "review"}
                or value.get("project_id") != project_id or value.get("task_id") != task_id
                or value.get("owner_thread") != request.get("owner_thread_id")
                or (value.get("worker_agent_id") and value.get("worker_agent_id") != worker_id)
                or value.get("input_version") != await workflow.input_version(snapshot, task)
                or not value.get("readonly_mechanism")
                or not isinstance(request.get("message"), str)
                or value.get("message_digest") != content_hash(request["message"])
                or value.get("status") not in {"pending", "confirmed"}
                or (value.get("claimed_run_id") and value.get("claimed_run_id") != run_id)
                or (value.get("status") == "confirmed" and value.get("run_id") != run_id)):
            raise PermissionError("只读请求与持久化意图不匹配")

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
                await authorize_readonly(request, claimed_project, prior_ref.get("task_id") or request.get("task_id"),
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
            if governed and request["pm_phase"] == "execution":
                await authorize_execution(request, ref, config)
                binding = await ProjectFolders(get_store()).verified_binding(ref.project_id)
                if binding is None or binding["directory"] != ref.cwd:
                    raise ValueError("项目目录未绑定或与现有会话不一致")
                # Never start a writable process in prepare: checkpoint failure
                # between graph nodes must not leave one running unattended.
                return {"worker_ref": ref.to_dict(), "owner_thread_id": request["owner_thread_id"],
                        "request": request, "pm_governed": True}
            worker = workers[ref.provider_id]
            if project_workers is not None:
                binding = await ProjectFolders(get_store()).verified_binding(ref.project_id)
                if binding is None or binding["directory"] != ref.cwd:
                    raise ValueError("项目目录未绑定或与现有会话不一致")
                if governed and request["pm_phase"] in {"discussion", "review"}:
                    if ref.isolation != "wsl-readonly":
                        raise PermissionError("现有会话没有可验证的只读边界")
                    worker = await project_workers.get_readonly(ref.provider_id, ref.cwd)
                else:
                    worker = await project_workers.get(ref.provider_id, ref.cwd)
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
            worker = workers[decision.provider_id]
            if project_workers is not None:
                binding = await ProjectFolders(get_store()).verified_binding(request["project_id"])
                if binding is None:
                    raise ValueError("项目尚未绑定文件夹")
                if governed and request["pm_phase"] in {"discussion", "review"}:
                    worker = await project_workers.get_readonly(decision.provider_id, binding["directory"])
                else:
                    worker = await project_workers.get(decision.provider_id, binding["directory"])
            ref = await worker.new(
                project_id=request["project_id"],
                task_id=request["task_id"],
                thread_id=config["configurable"]["thread_id"],
                role=request["agent_type"],
            )
        return {
            "worker_ref": ref.to_dict(),
            "owner_thread_id": request["owner_thread_id"],
            "request": request,
            "pm_governed": governed,
        }

    async def execute(state: ACPRunState, config: RunnableConfig):
        ref = WorkerRef(**state["worker_ref"])
        request = state["request"]
        if request.get("pm_governed"):
            if request.get("pm_phase") == "execution":
                await authorize_execution(request, ref, config)
            elif request.get("pm_phase") not in {"discussion", "review"} or ref.isolation != "wsl-readonly":
                raise PermissionError("当前会话没有可信的只读边界")
            else:
                async with project_write_lock(ref.project_id):
                    await authorize_readonly(request, ref.project_id, ref.task_id, ref.thread_id, config)
                    run_id = str(config.get("metadata", {}).get("run_id", ""))
                    if not run_id:
                        raise PermissionError("只读请求缺少官方 Run 标识")
                    namespace = ("deep_loopminder", "project_dispatch_intents", ref.project_id)
                    item = await get_store().aget((*namespace, "history"), request["pm_dispatch_id"])
                    claimed = {**item.value, "claimed_run_id": run_id}
                    await get_store().aput((*namespace, "history"), request["pm_dispatch_id"], claimed, index=False)
                    current = await get_store().aget(namespace, ref.task_id)
                    if current and current.value.get("intent_id") == request["pm_dispatch_id"]:
                        await get_store().aput(namespace, ref.task_id, claimed, index=False)
        chunks = []
        usage = None
        writer = get_stream_writer()

        async def notification(event):
            nonlocal usage
            update = event.update
            if update.session_update == "usage_update":
                usage = {
                    "used": update.used, "size": update.size,
                    "remaining": update.size - update.used if update.used <= update.size else None,
                    "source": "acp.session_update.usage_update",
                    "observed_at": datetime.now(UTC).isoformat(),
                }
            if (
                update.session_update == "agent_message_chunk"
                and update.content.type == "text"
            ):
                chunks.append(update.content.text)
            # Publish the native notification as-is through the official stream.
            writer(TrajectoryRecorder.clean_public(event.model_dump(mode="json", by_alias=True, exclude_none=True)))

        execution_phase = bool(request.get("pm_governed") and request.get("pm_phase") == "execution")
        quarantine = ("deep_loopminder", "project_worker_quarantine", ref.project_id)
        if execution_phase:
            ticket = project_workers.execution_ticket(ref, request["pm_dispatch_id"])
            async with project_write_lock(ref.project_id):
                await authorize_execution(request, ref, config)
                if await get_store().asearch(quarantine, limit=1):
                    raise PermissionError("项目中已有运行中或未确认停止的 Worker")
                # Reject pre-existing links before any writable process is
                # started. This scan is not an atomic filesystem snapshot and
                # must not be treated as proof of the OS isolation boundary.
                await ProjectFolders(get_store()).content_digest(ref.project_id)
                run_id = str(config.get("metadata", {}).get("run_id", ""))
                if not run_id:
                    raise PermissionError("开发请求缺少官方 Run 标识")
                namespace = ("deep_loopminder", "project_dispatch_intents", ref.project_id)
                history = await get_store().aget((*namespace, "history"), request["pm_dispatch_id"])
                claimed = {**history.value, "claimed_run_id": run_id}
                await get_store().aput((*namespace, "history"), request["pm_dispatch_id"], claimed, index=False)
                current = await get_store().aget(namespace, ref.task_id)
                if current and current.value.get("intent_id") == request["pm_dispatch_id"]:
                    await get_store().aput(namespace, ref.task_id, claimed, index=False)
                await get_store().aput(quarantine, ref.thread_id, {
                    "project_id": ref.project_id, "task_id": ref.task_id, "worker_ref": ref.to_dict(),
                    "intent_id": request["pm_dispatch_id"], "status": "shutdown_unconfirmed",
                    "ticket": ticket,
                }, index=False)
        try:
            worker = workers[ref.provider_id]
            if project_workers is not None:
                binding = await ProjectFolders(get_store()).verified_binding(ref.project_id)
                if binding is None or binding["directory"] != ref.cwd:
                    raise ValueError("项目目录未绑定或与现有会话不一致")
                if request.get("pm_governed") and request.get("pm_phase") in {"discussion", "review"}:
                    worker = await project_workers.get_readonly(ref.provider_id, ref.cwd)
                    if worker.readonly_enforcement is None:
                        raise PermissionError("只读执行能力不可验证")
                elif execution_phase:
                    worker = await project_workers.get_execution(ref, request["pm_dispatch_id"])
                    await worker.resume(ref)
                    await authorize_execution(request, ref, config)
                else:
                    worker = await project_workers.get(ref.provider_id, ref.cwd)
            if request.get("pm_governed") and request.get("pm_phase") in {"discussion", "review"}:
                # Recheck after connection setup as close as possible to ask.
                # This is not an atomic send against unrelated filesystem or
                # Store writers; stale results must still fail subsequent checks.
                await authorize_readonly(request, ref.project_id, ref.task_id, ref.thread_id, config)
            result = await worker.control(
                ref,
                "ask",
                {"text": state["request"]["message"]},
                on_update=notification,
            )
        finally:
            if execution_phase:
                await asyncio.shield(project_workers.release_execution(ref))
                async with project_write_lock(ref.project_id):
                    await get_store().adelete(quarantine, ref.thread_id)
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
            ]
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
