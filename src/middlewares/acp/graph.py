"""Bridge official background runs to SDK-managed ACP sessions."""

import json
from collections.abc import Mapping, Sequence
from typing import NotRequired

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph_sdk import get_client

from src.runtime.routing import RoleRequirement, WorkerCandidate, route_worker

from .control import ACPWorker, UnsupportedControl
from .correlation import WorkerRef


class ACPRunState(MessagesState):
    """Keep native identity and business correlation in the official checkpoint."""

    worker_ref: NotRequired[dict]
    owner_thread_id: NotRequired[str]
    request: NotRequired[dict]


def create_worker_graph(
    workers: Mapping[str, ACPWorker],
    candidates: Sequence[WorkerCandidate],
):
    """Compile a worker bridge; execution and persistence belong to Agent Server."""

    async def prepare(state: ACPRunState, config: RunnableConfig):
        message = next(
            m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)
        )
        request = json.loads(message.content)
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
            await workers[ref.provider_id].resume(ref)
        else:
            if request["operation"] != "spawn":
                raise RuntimeError(
                    "Native ACP reference is missing from the official checkpoint; cannot restore this conversation"
                )
            decision = route_worker(
                RoleRequirement(
                    request["agent_type"],
                    frozenset(request.get("required_capabilities", ())),
                ),
                [
                    candidate
                    for candidate in candidates
                    if workers[candidate.provider_id].process.returncode is None
                ],
            )
            if decision.provider_id is None:
                raise RuntimeError(
                    decision.reason + "; rejected_reasons="
                    + json.dumps(dict(decision.rejected_reasons), ensure_ascii=False)
                )
            worker = workers[decision.provider_id]
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
        }

    async def execute(state: ACPRunState, config: RunnableConfig):
        ref = WorkerRef(**state["worker_ref"])
        chunks = []
        writer = get_stream_writer()

        async def notification(event):
            update = event.update
            if (
                update.session_update == "agent_message_chunk"
                and update.content.type == "text"
            ):
                chunks.append(update.content.text)
            # Publish the native notification as-is through the official stream.
            writer(event.model_dump(mode="json", by_alias=True, exclude_none=True))

        result = await workers[ref.provider_id].control(
            ref,
            "ask",
            {"text": state["request"]["message"]},
            on_update=notification,
        )
        return {
            "messages": [
                AIMessage(
                    content="".join(chunks),
                    additional_kwargs={
                        "acp_stop_reason": result.stop_reason,
                        "acp_run_id": str(config.get("metadata", {}).get("run_id", "")),
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


def create_control_graph(workers: Mapping[str, ACPWorker]):
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
        worker = workers.get(ref.provider_id)
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
