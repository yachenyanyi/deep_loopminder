"""Subagent tools backed by official Agent Server runs and ACP sessions."""

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from functools import wraps
from typing import Annotated, Literal, NotRequired

import httpx
from deepagents.middleware.async_subagents import (
    AsyncSubAgent,
    AsyncSubAgentMiddleware,
    AsyncSubAgentState,
)
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.types import Command
from langgraph_sdk import get_client
from pydantic import Field

from src.runtime.routing import RoleRequirement, WorkerCandidate, route_worker

_TERMINAL = frozenset({"success", "error", "cancelled", "interrupted", "timeout"})
_MESSAGE = Annotated[str, Field(min_length=1, max_length=100000)]
_TARGET = Annotated[str, Field(min_length=1, max_length=128)]
_INSTRUCTIONS = """使用 spawn_agent 委派自主工作的负责人，以返回的 agent_id 或 task_name 跟进。
派工说明写业务目标、必要范围和验收要求，默认中文；技术方案、命令和内部拆分交给 Worker。
followup_task 在同一原生会话继续，保留上下文，只需交代新增信息；send_message 用于调整正在执行的工作。
Worker 不继承父 Agent 的工具。只能要求其使用能力目录和角色说明中明确提供的能力；缺少工具时报告阻碍，不能研究宿主接口或直写 Store。
具备原生子代理能力的执行端可自主分工，项目级 Worker 对其内部工作负责；父 Agent 不直接控制内部子代理。
成果通过会话交接，项目集成的 wait_agent 自动保存最终结果，不要求 Worker 调用父 Agent 的制品工具。
wait_agent 默认只领取未读结果。历史重读必须明确指定单个 target 和 run_id；超时仅结束等待，不取消 Worker。
根据 errors 和 next_action 处理问题，不盲目重试写操作；Run 成功不等于任务验收或人工批准。"""


def _merge(existing: dict | None, update: dict) -> dict:
    """Merge labels or delivery receipts; neither is an execution catalog."""
    return {**(existing or {}), **update}


class ACPState(AsyncSubAgentState):
    """Keep business names and delivery receipts alongside official task state."""

    acp_task_names: Annotated[NotRequired[dict[str, str]], _merge]
    acp_delivered: Annotated[NotRequired[dict[str, bool]], _merge]


class _ToolFailure(Exception):
    """Carry an actionable tool error without aborting the parent agent."""

    def __init__(self, code, message, *, retryable=False, next_action=None, **details):
        """Describe the failure and safe caller recovery."""
        super().__init__(message)
        self.payload = {
            "code": code,
            "message": message,
            "retryable": retryable,
            "next_action": next_action,
            **details,
        }


def _guard(function):
    """Convert expected operational failures, preserving cancellation and bugs."""

    @wraps(function)
    async def guarded(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except _ToolFailure as error:
            return {"ok": False, "error": error.payload}

    return guarded


class ACPMiddleware(AsyncSubAgentMiddleware):
    """Expose six collaboration tools over official threads, runs and history.

    Use an async Agent entrypoint and a persistent parent thread_id. Connections,
    provider permissions and endpoint authentication are trusted configuration.
    The only execution catalog is the inherited official async_tasks state.
    """

    state_schema = ACPState

    def __init__(
        self,
        *,
        async_subagents: list[AsyncSubAgent],
        control_graph_id: str = "acp_control",
        default_agent_type: str | None = None,
        request_timeout: float = 15,
        candidate_source: Callable[[], Sequence[WorkerCandidate]] | None = None,
    ):
        """Configure trusted role endpoints and bound individual SDK requests."""
        super().__init__(async_subagents=async_subagents, system_prompt=_INSTRUCTIONS)
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self._specs = {spec["name"]: spec for spec in async_subagents}
        if default_agent_type is not None and default_agent_type not in self._specs:
            raise ValueError("default_agent_type must name a configured role")
        self._default = default_agent_type or (
            next(iter(self._specs)) if len(self._specs) == 1 else None
        )
        self._start = next(t for t in self.tools if t.name == "start_async_task")
        self._control_graph_id = control_graph_id
        self._request_timeout = request_timeout
        self._candidate_source = candidate_source

        @tool
        @_guard
        async def spawn_agent(
            task_name: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")],
            message: _MESSAGE,
            runtime: ToolRuntime,
            agent_type: str | None = None,
            task_id: Annotated[
                str | None, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
            ] = None,
        ) -> dict | Command:
            """Start a background subagent. task_name is a unique worker alias (letters/digits/underscore/hyphen, case-sensitive). task_id optionally binds an existing business task; defaults to task_name. For an independent review use a new alias with the same task_id. Omit agent_type only when a default role is configured."""
            owner = self._owner(runtime)
            self._text(message)
            role = agent_type or self._default
            if role not in self._specs:
                raise _ToolFailure(
                    "unknown_agent_type",
                    "Choose a configured agent_type.",
                    next_action="spawn_agent",
                    available_agent_types=list(self._specs),
                )
            matches = [
                key
                for key, name in runtime.state.get("acp_task_names", {}).items()
                if name == task_name
            ]
            if matches:
                raise _ToolFailure(
                    "task_name_exists",
                    "This name already identifies a subagent; use followup_task or choose a new name.",
                    next_action="followup_task",
                    agent_ids=matches,
                )
            calls = (
                getattr(runtime.state.get("messages", [])[-1], "tool_calls", [])
                if runtime.state.get("messages")
                else []
            )
            if (
                sum(
                    call["name"] == "spawn_agent"
                    and call["args"].get("task_name") == task_name
                    for call in calls
                )
                > 1
            ):
                raise _ToolFailure(
                    "duplicate_parallel_name",
                    "Parallel spawns must use distinct task_name values.",
                    next_action="spawn_agent",
                )
            context = runtime.config.get("configurable", {})
            if self._candidate_source is not None:
                decision = route_worker(
                    RoleRequirement(
                        role, frozenset(context.get("required_capabilities", ()))
                    ),
                    self._candidate_source(),
                )
                if decision.provider_id is None:
                    raise _ToolFailure(
                        "worker_requirements_unmatched",
                        "任务要求无法匹配当前角色/能力目录；没有创建 Worker Run。请停止本轮并报告配置或要求不一致，不能反复续问。",
                        next_action="stop_and_report",
                        fatal=True,
                        rejected_reasons=dict(decision.rejected_reasons),
                        available_agent_types=self._role_catalog(),
                    )
            envelope = {
                "operation": "spawn",
                "message": message,
                "agent_type": role,
                "task_name": task_name,
                "owner_thread_id": owner,
                "project_id": context.get("project_id") or owner,
                "task_id": context.get("task_id") or task_id or task_name,
                "required_capabilities": context.get("required_capabilities", []),
            }
            command = await self._api(
                self._start.coroutine(
                    description=json.dumps(envelope, ensure_ascii=False),
                    subagent_type=role,
                    runtime=runtime,
                ),
                mutation=True,
            )
            if not isinstance(command, Command):
                # The official start tool already logs the original SDK exception.
                raise _ToolFailure(
                    "start_failed",
                    "Agent Server could not confirm creation. Inspect the server before retrying.",
                    next_action="list_agents",
                    outcome_unknown=True,
                )
            key, tracked = next(iter(command.update["async_tasks"].items()))
            command.update["acp_task_names"] = {key: task_name}
            return self._message(
                command,
                runtime,
                {
                    **self._entry(runtime, key, tracked, fresh=False),
                    "task_name": task_name,
                    "next_action": "wait_agent",
                },
            )

        spawn_agent.description += " Available roles: " + "; ".join(
            f"{name}: {spec['description']}" for name, spec in self._specs.items()
        )

        @tool
        @_guard
        async def followup_task(
            target: _TARGET, message: _MESSAGE, runtime: ToolRuntime
        ) -> dict | Command:
            """在同一原生会话继续工作，保留已有上下文；message只补变化和待办，不重贴完整简报。target接受agent_id或task_name；历史结果仍由wait_agent领取。"""
            key, tracked = self._resolve(runtime, target)
            self._text(message)
            current = await self._api(
                self._client(tracked).runs.get(key, tracked["run_id"])
            )
            if current["status"] in {"error", "timeout", "interrupted", "cancelled"}:
                snapshot = await self._api(self._client(tracked).threads.get_state(key))
                values = snapshot.get("values", {})
                if values.get("owner_thread_id") not in (None, self._owner(runtime)):
                    raise _ToolFailure(
                        "foreign_session",
                        "The remote session belongs to another parent.",
                    )
                if not values.get("worker_ref"):
                    raise _ToolFailure(
                        "session_not_created",
                        "上轮在建立原生会话前失败，没有可续问的会话；未创建新 Run。",
                        next_action="stop_and_report",
                        fatal=True,
                    )
            envelope = {
                "operation": "followup",
                "message": message,
                "agent_type": tracked["agent_name"],
                "owner_thread_id": self._owner(runtime),
            }
            # Business identity stays in the worker checkpoint, independent of
            # changes to the parent invocation's project/task config.
            run = await self._api(
                self._client(tracked).runs.create(
                    thread_id=key,
                    assistant_id=self._specs[tracked["agent_name"]]["graph_id"],
                    input={
                        "messages": [
                            {
                                "role": "user",
                                "content": json.dumps(envelope, ensure_ascii=False),
                            }
                        ]
                    },
                    multitask_strategy="enqueue",
                ),
                mutation=True,
            )
            updated = self._updated(tracked, run["status"], run_id=run["run_id"])
            return self._message(
                Command(update={"async_tasks": {key: updated}}),
                runtime,
                {
                    **self._entry(runtime, key, updated),
                    "queued": run["status"] == "pending",
                    "next_action": "wait_agent",
                },
            )

        @tool
        @_guard
        async def send_message(
            target: _TARGET, message: _MESSAGE, runtime: ToolRuntime
        ) -> dict:
            """Inject instructions into active work using native steer. Idle/unsupported sessions suggest followup_task; never simulate a mailbox."""
            key, tracked = self._resolve(runtime, target)
            self._text(message)
            result = await self._api(
                self._client(tracked).runs.wait(
                    None,
                    self._control_graph_id,
                    input={
                        "target": key,
                        "owner_thread_id": self._owner(runtime),
                        "message": message,
                    },
                ),
                mutation=True,
            )
            if "__error__" in result:
                raise _ToolFailure(
                    "control_failed",
                    "The control graph failed; inspect Agent Server logs.",
                    next_action="list_agents",
                )
            outcome = json.loads(result["messages"][-1]["content"])
            return {
                "ok": outcome.get("outcome") == "injected",
                "agent_id": key,
                **outcome,
            }

        @tool
        @_guard
        async def list_agents(
            runtime: ToolRuntime,
            path_prefix: str | None = None,
            status: Literal[
                "pending",
                "running",
                "success",
                "error",
                "interrupted",
                "timeout",
                "cancelled",
            ]
            | None = None,
        ) -> dict | Command:
            """List this parent's subagents; filter task_name prefix and fresh run status. Connection failures are explicit; cached status is marked stale."""
            self._owner(runtime)
            keys = [
                key
                for key in runtime.state.get("async_tasks", {})
                if path_prefix is None
                or runtime.state.get("acp_task_names", {})
                .get(key, "")
                .startswith(path_prefix)
            ]
            snapshots = await self._snapshots(runtime, keys)
            agents = [
                self._present(runtime, key, data) for key, data in snapshots.items()
            ]
            if status:
                agents = [
                    entry
                    for entry in agents
                    if entry["status"] == status or not entry["status_fresh"]
                ]
            return self._message(
                self._updates(snapshots),
                runtime,
                {
                    "agents": agents,
                    "errors": self._errors(snapshots),
                    "available_agent_types": list(self._specs),
                    "role_capabilities": self._role_catalog(),
                },
            )

        @tool
        @_guard
        async def wait_agent(
            runtime: ToolRuntime,
            timeout_ms: Annotated[int, Field(ge=0, le=60000)] = 30000,
            targets: Annotated[
                list[_TARGET] | None, Field(min_length=1, max_length=8)
            ] = None,
            include_completed: bool = False,
            run_id: str | None = None,
        ) -> dict | Command:
            """等待并领取未读结果，每个Run默认返回一次。正常跟进不要设置include_completed；历史重读必须提供单个target和run_id。timeout_ms=0立即查询；超时不取消远端工作。"""
            self._owner(runtime)
            history_notice = None
            if include_completed and run_id is None:
                # 兼容旧会话习惯：不重复投递旧结果，也不因无副作用的读取选项中断项目。
                include_completed = False
                history_notice = "未指定run_id，已按默认未读结果模式处理；需要历史重读时请指定单个target和run_id。"
            keys = (
                list(runtime.state.get("async_tasks", {}))
                if targets is None
                else list(
                    dict.fromkeys(
                        self._resolve(runtime, target)[0] for target in targets
                    )
                )
            )
            snapshots = await self._snapshots(runtime, keys)
            if run_id is not None:
                if len(keys) != 1:
                    raise _ToolFailure(
                        "invalid_run_target",
                        "run_id requires exactly one target.",
                        next_action="wait_agent",
                    )
                data = snapshots[keys[0]]
                if not data.get("error") and not any(
                    run["run_id"] == run_id for run in data["runs"]
                ):
                    raise _ToolFailure(
                        "unknown_run",
                        "run_id does not belong to the selected agent.",
                        next_action="list_agents",
                    )
                data["runs"] = [run for run in data["runs"] if run["run_id"] == run_id]
                include_completed = True
            receipts, completed, errors = await self._results(
                runtime, snapshots, include_completed
            )
            active = [
                (key, run, data["task"])
                for key, data in snapshots.items()
                if not data.get("error")
                for run in data["runs"]
                if run["status"] not in _TERMINAL
            ]
            timed_out = False
            if not completed and not errors and active and timeout_ms:
                joins = [
                    asyncio.create_task(
                        self._client(task).runs.join(key, run["run_id"])
                    )
                    for key, run, task in active
                ]
                waiter_errors = []
                try:
                    done, _ = await asyncio.wait(
                        joins,
                        timeout=timeout_ms / 1000,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    # Failed waiters are not proof of remote completion.
                    for future in done:
                        try:
                            future.result()
                        except (httpx.HTTPError, TimeoutError):
                            waiter_errors.append(
                                {
                                    "code": "wait_failed",
                                    "message": "Agent Server could not complete the wait; remote work was not cancelled.",
                                    "retryable": True,
                                    "next_action": "list_agents",
                                }
                            )
                    timed_out = not done
                finally:
                    for future in joins:
                        if not future.done():
                            future.cancel()
                    await asyncio.gather(*joins, return_exceptions=True)
                snapshots = await self._snapshots(runtime, keys)
                if run_id is not None:
                    snapshots[keys[0]]["runs"] = [
                        run
                        for run in snapshots[keys[0]]["runs"]
                        if run["run_id"] == run_id
                    ]
                receipts, completed, errors = await self._results(
                    runtime, snapshots, include_completed
                )
                errors.extend(waiter_errors)
                active = [
                    (key, run, data["task"])
                    for key, data in snapshots.items()
                    if not data.get("error")
                    for run in data["runs"]
                    if run["status"] not in _TERMINAL
                ]
                timed_out = timed_out and not completed
            elif not completed and active and timeout_ms == 0:
                timed_out = True
            command = self._updates(snapshots)
            command.update["acp_delivered"] = receipts
            has_more = any(
                run["status"] in _TERMINAL
                and run["run_id"] not in receipts
                and (
                    include_completed
                    or not runtime.state.get("acp_delivered", {}).get(run["run_id"])
                )
                for data in snapshots.values()
                for run in data["runs"]
            )
            return self._message(
                command,
                runtime,
                {
                    "completed": completed,
                    "timed_out": timed_out,
                    "agents": [
                        self._present(runtime, key, data, receipts)
                        for key, data in snapshots.items()
                    ],
                    "errors": errors,
                    "has_active": bool(active),
                    "has_more_results": has_more,
                    **({"notice": history_notice} if history_notice else {}),
                },
            )

        @tool
        @_guard
        async def interrupt_agent(
            target: _TARGET, runtime: ToolRuntime
        ) -> dict | Command:
            """Cancel queued and active turns, preserve native history, and report observed status. Repeating after completion is a harmless no-op."""
            key, tracked = self._resolve(runtime, target)
            client = self._client(tracked)
            runs = await self._runs(tracked)
            active = [run for run in runs if run["status"] not in _TERMINAL]
            cancelled, errors = [], []
            # Cancel the queue first so stopping active work does not start it.
            for run in sorted(active, key=lambda run: run["status"] != "pending"):
                try:
                    await self._api(
                        client.runs.cancel(key, run["run_id"], wait=False),
                        mutation=True,
                    )
                    cancelled.append(run["run_id"])
                except _ToolFailure as error:
                    if error.payload.get("http_status") != 409:
                        errors.append({"run_id": run["run_id"], **error.payload})
            converged = True
            if cancelled:
                joins = [
                    asyncio.create_task(client.runs.join(key, run_id))
                    for run_id in cancelled
                ]
                try:
                    await self._api(asyncio.gather(*joins))
                except _ToolFailure as error:
                    converged = False
                    errors.append(error.payload)
                finally:
                    for future in joins:
                        if not future.done():
                            future.cancel()
                    await asyncio.gather(*joins, return_exceptions=True)
            snapshots = await self._snapshots(runtime, [key])
            observed = snapshots[key]
            remaining = [
                run["run_id"]
                for run in observed["runs"]
                if run["status"] not in _TERMINAL
            ]
            complete = (
                converged and not remaining and not observed.get("error") and not errors
            )
            return self._message(
                self._updates(snapshots),
                runtime,
                {
                    **self._present(runtime, key, observed),
                    "cancellation_requested": bool(cancelled),
                    "cancelled_run_ids": cancelled,
                    "interruption_complete": bool(complete),
                    "remaining_run_ids": remaining,
                    "errors": errors + self._errors(snapshots),
                    "next_action": "followup_task" if complete else "list_agents",
                },
            )

        self.tools = [
            spawn_agent,
            followup_task,
            send_message,
            wait_agent,
            list_agents,
            interrupt_agent,
        ]

    def _role_catalog(self):
        """按业务角色公开精确能力集合，提供方与认证仍由宿主管理。"""
        candidates = (
            self._candidate_source() if self._candidate_source is not None else ()
        )
        return [
            {
                "agent_type": name,
                "description": spec["description"],
                "tool_boundary": "独立原生工具集，不继承PM工具；通过会话返回成果，不直接访问宿主Store。",
                "session": "followup_task保留同一会话上下文，继续时只交代变化；内部子代理能力以角色说明和执行端实际工具为准。",
                "available_capability_sets": [
                    sorted(candidate.reported_capabilities)
                    for candidate in candidates
                    if name in candidate.roles and candidate.runtime_available
                ],
            }
            for name, spec in self._specs.items()
        ]

    async def awrap_model_call(self, request, handler):
        """按现有官方状态提供角色及会话摘要，不另建一份协作记忆。"""
        if self._specs:
            text = (
                "当前可用角色及精确能力目录（required_capabilities 只能选同一集合内的原始标识；"
                "不需要额外筛选时留空，不能自造同义名称；能力匹配不构成权限授权）：\n"
                + json.dumps(self._role_catalog(), ensure_ascii=False)
            )
            sessions = [
                {
                    "target": request.state.get("acp_task_names", {}).get(key, key),
                    "role": tracked["agent_name"],
                    "latest_run_id": tracked["run_id"],
                    "last_observed_status": tracked["status"],
                    "result_collected": bool(
                        request.state.get("acp_delivered", {}).get(tracked["run_id"])
                    ),
                }
                for key, tracked in request.state.get("async_tasks", {}).items()
            ]
            text += (
                "\n已有负责人会话（状态为最后一次观察，非实时探测；续接用followup_task，不重新介绍全部背景）：\n"
                + json.dumps(sessions, ensure_ascii=False)
            )
            system = request.system_message
            system = (
                system.model_copy(
                    update={
                        "content": [
                            *system.content_blocks,
                            {"type": "text", "text": text},
                        ]
                    }
                )
                if system
                else SystemMessage(content=text)
            )
            request = request.override(system_message=system)
        return await super().awrap_model_call(request, handler)

    @staticmethod
    def _owner(runtime: ToolRuntime) -> str:
        """Use the execution identity, never a model-supplied owner."""
        owner = runtime.execution_info.thread_id
        if not owner:
            raise _ToolFailure(
                "missing_thread_id",
                "A persistent parent config.configurable.thread_id is required.",
            )
        return owner

    @staticmethod
    def _text(message: str):
        """Reject empty instructions before contacting a provider."""
        if not message.strip():
            raise _ToolFailure(
                "empty_message", "message must contain non-whitespace text."
            )

    def _resolve(self, runtime: ToolRuntime, target: str) -> tuple[str, dict]:
        """Resolve IDs or unambiguous business names within the caller's tasks."""
        self._owner(runtime)
        tasks = runtime.state.get("async_tasks", {})
        if target in tasks:
            return target, tasks[target]
        matches = [
            key
            for key, name in runtime.state.get("acp_task_names", {}).items()
            if name == target and key in tasks
        ]
        if len(matches) == 1:
            return matches[0], tasks[matches[0]]
        raise _ToolFailure(
            "ambiguous_target" if matches else "unknown_target",
            "Use an agent_id from list_agents; target must belong to this parent.",
            next_action="list_agents",
            agent_ids=matches,
        )

    def _client(self, task: dict):
        """Use the trusted endpoint and the public SDK's authentication."""
        spec = self._specs.get(task["agent_name"])
        if spec is None:
            raise _ToolFailure(
                "role_unavailable",
                "This subagent's role is no longer configured; restore its endpoint configuration.",
            )
        return get_client(
            url=spec.get("url"),
            headers={"x-auth-scheme": "langsmith", **(spec.get("headers") or {})},
        )

    async def _api(self, request, *, mutation=False):
        """Bound requests and distinguish retryable reads from unknown mutations."""
        try:
            async with asyncio.timeout(self._request_timeout):
                return await request
        except (TimeoutError, httpx.TimeoutException) as error:
            raise _ToolFailure(
                "request_timeout",
                "Agent Server did not respond before the request deadline.",
                retryable=not mutation,
                next_action="list_agents",
                outcome_unknown=mutation,
            ) from error
        except httpx.HTTPError as error:
            status = (
                error.response.status_code
                if isinstance(error, httpx.HTTPStatusError)
                and error.response is not None
                else None
            )
            raise _ToolFailure(
                "server_unavailable" if status is None else "server_rejected",
                "Agent Server request failed; check endpoint, authentication and deployment.",
                retryable=not mutation
                and (status is None or status >= 500 or status == 429),
                next_action="list_agents",
                http_status=status,
                outcome_unknown=mutation and (status is None or status >= 500),
            ) from error

    async def _runs(self, tracked: dict) -> list[dict]:
        """Read the official run catalog, including queued and older turns."""
        client = self._client(tracked)
        runs, offset = [], 0
        while True:
            batch = await self._api(
                client.runs.list(tracked["thread_id"], limit=100, offset=offset)
            )
            runs.extend(batch)
            if len(batch) < 100:
                break
            offset += len(batch)
        if not any(run["run_id"] == tracked["run_id"] for run in runs):
            runs.append(
                dict(
                    await self._api(
                        client.runs.get(tracked["thread_id"], tracked["run_id"])
                    )
                )
            )
            runs[-1].setdefault("run_id", tracked["run_id"])
        return runs

    async def _snapshots(self, runtime, keys):
        """Refresh concurrently; preserve successful observations on partial failure."""

        async def inspect(key):
            task = runtime.state["async_tasks"][key]
            try:
                runs = await self._runs(task)
                current = next(run for run in runs if run["run_id"] == task["run_id"])
                return key, {
                    "task": self._updated(task, current["status"]),
                    "runs": runs,
                }
            except _ToolFailure as error:
                return key, {"task": task, "runs": [], "error": error.payload}

        return dict(await asyncio.gather(*(inspect(key) for key in keys)))

    async def _results(self, runtime, snapshots, include_completed):
        """Deliver at most eight run results; keep unavailable results unread."""
        delivered = runtime.state.get("acp_delivered", {})
        receipts, completed, errors = {}, [], self._errors(snapshots)
        for key, data in snapshots.items():
            runs = sorted(data["runs"], key=lambda run: str(run.get("created_at", "")))
            for run in runs:
                run_id = run["run_id"]
                if run["status"] not in _TERMINAL or (
                    delivered.get(run_id) and not include_completed
                ):
                    continue
                if len(completed) >= 8:
                    break
                try:
                    completed.append(
                        await self._result(runtime, key, data["task"], run)
                    )
                    receipts[run_id] = True
                except _ToolFailure as error:
                    errors.append({"agent_id": key, "run_id": run_id, **error.payload})
        return receipts, completed, errors

    async def _result(self, runtime, key, task, run):
        """Read the final checkpoint for this run, never the thread's latest reply."""
        history = await self._api(
            self._client(task).threads.get_history(
                key,
                metadata={"run_id": run["run_id"]},
                limit=1,
            )
        )
        result = {
            **self._entry(runtime, key, task),
            "run_id": run["run_id"],
            "status": run["status"],
        }
        snapshot = history[0] if history else None
        if snapshot and snapshot["values"].get("owner_thread_id") not in (
            None,
            self._owner(runtime),
        ):
            raise _ToolFailure(
                "foreign_session", "The remote session belongs to another parent."
            )
        if run["status"] != "success":
            has_session = bool(
                snapshot and snapshot.get("values", {}).get("worker_ref")
            )
            result["error"] = {
                "code": "run_" + run["status"]
                if has_session
                else "session_not_created",
                "message": "Worker execution ended without success."
                if has_session
                else "原生会话未创建，不能续问；需要先排查派工或连接错误。",
                "retryable": has_session,
                "fatal": not has_session,
                "next_action": "followup_task" if has_session else "stop_and_report",
            }
            if snapshot:
                details = [
                    str(item["error"])[:2000]
                    for item in snapshot.get("tasks", [])
                    if item.get("error")
                ]
                if details:
                    result["error"]["details"] = details
            return result
        if not snapshot or snapshot.get("next"):
            raise _ToolFailure(
                "result_unavailable",
                "No final checkpoint for this run; result remains unread.",
                retryable=True,
                next_action="wait_agent",
            )
        if snapshot["values"].get("owner_thread_id") != self._owner(runtime):
            raise _ToolFailure(
                "foreign_session", "The remote session belongs to another parent."
            )
        messages = snapshot["values"].get("messages", [])
        reply = next(
            (
                m
                for m in reversed(messages)
                if m.get("type") == "ai" or m.get("role") == "assistant"
            ),
            None,
        )
        if (
            reply is None
            or reply.get("additional_kwargs", {}).get("acp_run_id", run["run_id"])
            != run["run_id"]
        ):
            raise _ToolFailure(
                "result_unavailable",
                "The checkpoint has no reply from this run; result remains unread.",
                retryable=True,
                next_action="wait_agent",
            )
        result.update(
            {
                "result": reply["content"],
                "stop_reason": reply.get("additional_kwargs", {}).get(
                    "acp_stop_reason"
                ),
                "checkpoint": snapshot.get("checkpoint"),
            }
        )
        return result

    @staticmethod
    def _updated(task, status, **changes):
        """Preserve official task fields while recording a fresh observation."""
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            **task,
            "status": status,
            "last_checked_at": now,
            "last_updated_at": now
            if status != task["status"] or changes
            else task["last_updated_at"],
            **changes,
        }

    @staticmethod
    def _entry(runtime, key, task, *, fresh=True):
        """Present stable identity, current run and observation freshness."""
        return {
            "agent_id": key,
            "task_name": runtime.state.get("acp_task_names", {}).get(key),
            "agent_type": task["agent_name"],
            "run_id": task["run_id"],
            "status": task["status"],
            "status_fresh": fresh,
            "created_at": task["created_at"],
            "last_checked_at": task["last_checked_at"],
        }

    def _present(self, runtime, key, data, receipts=None):
        """Show actual running work even when the tracked latest turn is queued."""
        entry = self._entry(runtime, key, data["task"], fresh=not data.get("error"))
        running = [run["run_id"] for run in data["runs"] if run["status"] == "running"]
        queued = [run["run_id"] for run in data["runs"] if run["status"] == "pending"]
        delivered = {**runtime.state.get("acp_delivered", {}), **(receipts or {})}
        entry.update(
            {
                "latest_run_status": entry["status"],
                "running_run_ids": running,
                "queued_run_ids": queued,
                "unread_results": sum(
                    run["status"] in _TERMINAL and not delivered.get(run["run_id"])
                    for run in data["runs"]
                ),
            }
        )
        if running or queued:
            entry["status"] = "running" if running else "pending"
        return entry

    @staticmethod
    def _errors(snapshots):
        """Expose per-agent failures without suppressing healthy siblings."""
        return [
            {"agent_id": key, **data["error"]}
            for key, data in snapshots.items()
            if data.get("error")
        ]

    @staticmethod
    def _updates(snapshots):
        """Update only observations successfully read from Agent Server."""
        return Command(
            update={
                "async_tasks": {
                    key: data["task"]
                    for key, data in snapshots.items()
                    if not data.get("error")
                }
            }
        )

    @staticmethod
    def _message(command: Command, runtime: ToolRuntime, payload: dict) -> Command:
        """Keep official state changes and a consistent tool result envelope."""
        command.update["messages"] = [
            ToolMessage(
                content=json.dumps({"ok": True, **payload}, ensure_ascii=False),
                tool_call_id=runtime.tool_call_id,
            )
        ]
        return command
