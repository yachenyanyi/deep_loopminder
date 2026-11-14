"""宿主触发的领取 Run：Worker Run 结束而 PM 空闲时，请 PM 领取结果。

官方 async 子代理通知规则（deepagents/async-subagents）：supervisor 忙时，任务
通知在下一次模型调用前送达；空闲 supervisor 只有 deep-agent 子代理才会唤醒，
而 acp_worker 是普通 StateGraph。官方唤醒（_awake_parent）创建的通知 Run 也不
携带本项目按 Run 传入的 configurable（project_id 等），PM 在其中无法操作项目。
因此空闲时段的"完成通知到达再领取"由宿主补位：观察各项目 PM 会话上的官方后台
任务，Run 结束且未领取且 PM 空闲时，创建一个携带项目 configurable 的领取 Run，
PM 在其中领取并继续。候选不只看缓存的未完成状态：官方忙时通知可能已把缓存
状态标成终态、但 wait_agent 尚未领取（acp_delivered 无记录），这种任务同样
需要宿主触发，否则结果一直无人领取；权威门槛是 fresh runs.get 的终态。
busy 会话不动（官方拉取在下一次模型调用前投递）；等待人工
决策的会话不动（用户答复后的拉取会投递）；Worker 等待输入（waiting）无事可领，
同样跳过。领取 Run 明确结束后，仍未领取的结果最多再唤醒一次；状态不可查询
时不重试，避免在领取请求结果未知时重复派发。
"""

import asyncio
import logging
import os

from langgraph_sdk import get_client

from .context import THREADS as _THREADS
from .context import get_store

_LOGGER = logging.getLogger(__name__)

_TERMINAL = frozenset({"success", "error", "cancelled", "interrupted", "timeout"})
# 与官方 _awake_parent 的 _DELIVERABLE_THREAD_STATUSES 同一条规则：不惊扰忙碌
# 或等待人工决策的父会话。
_DELIVERABLE_THREAD_STATUSES = frozenset({"idle", "error"})
_MAX_TRIGGERED = 4096
_MAX_WAKE_ATTEMPTS = 2


def _poll_seconds():
    """Resolve the observation interval; invalid configuration falls back to 5s."""
    from src.middlewares.agent.pm_settings import current

    settings = current()
    try:
        configured = (settings.read().get("runtime", {}).get("claim_poll_seconds", 5) if settings
                      else os.getenv("PM_CLAIM_POLL_SECONDS", "5"))
        value = float(configured)
    except (ValueError, TypeError):
        return 5.0
    return value if value > 0 else 5.0


def _claim_message(entries, names):
    """One host notice per pass, listing every newly finished unclaimed run."""
    lines = [
        f"- {names.get(task_id) or task['agent_name']}（run_id {task['run_id']}，status {status}）"
        for task_id, task, status in entries
    ]
    return (
        "[宿主通知] 以下后台任务的 Run 已结束且结果尚未领取：\n"
        + "\n".join(lines)
        + "\n请用 wait_agent 领取未读结果并继续推进；这是宿主状态提示，不是新的业务指令。"
    )


def _outcome(run_status, thread_status):
    """官方 _task_status 的领取视角：返回可领取的状态，或 None（无事可领）。

    成功的 Run 还要看 Worker 线程：interrupted 表示负责人在等输入，busy 表示
    线程上还有更新的 Run 在工作——两者都没有最终结果可领；error 沿用官方映射
    报告为 error，其余成功视为 success。
    """
    if run_status == "interrupted":
        return "cancelled"
    if run_status not in _TERMINAL:
        return None
    if run_status != "success":
        return run_status
    if thread_status in {"interrupted", "busy"}:
        return None
    if thread_status == "error":
        return "error"
    return "success"


class ClaimWatcher:
    """Find finished, unclaimed worker runs and wake an idle PM to claim them."""

    def __init__(self, store=None):
        """Leave the Store unset to resolve it lazily; startup ordering cannot break the loop."""
        self.store = store
        self._triggered: dict[str, dict] = {}

    async def _already_triggered(self, client, thread_id: str, run_id: str) -> bool:
        """Wait for a known claim run to finish before allowing a bounded retry."""
        entry = self._triggered.get(run_id)
        if entry is None:
            return False
        claim_id = entry.get("claim_run_id")
        if not claim_id or entry["attempts"] >= _MAX_WAKE_ATTEMPTS:
            return True
        try:
            claim = await client.runs.get(thread_id, claim_id)
        except Exception:  # noqa: BLE001  # unknown outcome must not create a duplicate wake
            return True
        return claim.get("status") not in _TERMINAL

    def _mark_triggered(self, run_id: str, claim_run_id: str | None) -> None:
        """Track the claim run and limit retries for each worker result."""
        previous = self._triggered.get(run_id, {})
        self._triggered[run_id] = {"claim_run_id": claim_run_id,
                                   "attempts": previous.get("attempts", 0) + 1}
        while len(self._triggered) > _MAX_TRIGGERED:
            self._triggered.pop(next(iter(self._triggered)))

    async def _claimable(self, client, thread_id: str, values: dict) -> list[tuple[str, dict, str]]:
        """Finished, unclaimed tracked runs on an idle PM thread; unreadable facts are skipped.

        The cached status is only a filter, never the verdict: an official
        busy-time notification may have already marked the cache terminal while
        the result stayed unclaimed (wait_agent never ran). The authoritative
        gate is the fresh runs.get below; already-delivered runs are skipped
        without the extra request.
        """
        try:
            thread = await client.threads.get(thread_id)
        except Exception:  # noqa: BLE001  # 404 after a dev restart or transient errors: skip this pass
            return []
        if thread.get("status") not in _DELIVERABLE_THREAD_STATUSES:
            return []
        tasks = values.get("async_tasks") or {}
        delivered = values.get("acp_delivered") or {}
        entries = []
        for task_id, task in tasks.items():
            if not isinstance(task, dict):
                continue
            run_id = task.get("run_id")
            if not run_id or delivered.get(run_id):
                continue
            if await self._already_triggered(client, thread_id, run_id):
                continue
            try:
                run = await client.runs.get(task_id, run_id)
            except Exception:  # noqa: BLE001  # unreadable now: checked again next pass
                continue
            worker_status = None
            if run.get("status") == "success":
                try:
                    worker_thread = await client.threads.get(task_id)
                except Exception:  # noqa: BLE001  # unreadable now: checked again next pass
                    continue
                worker_status = worker_thread.get("status")
            outcome = _outcome(run.get("status"), worker_status)
            if outcome is not None:
                entries.append((task_id, task, outcome))
        return entries

    async def _wake(self, client, project_id: str, thread_id: str, entries, names) -> bool:
        """Start one claim run; an unknown create outcome is not retried blindly."""
        try:
            claim = await client.runs.create(
                thread_id,
                "pm_agent",
                input={"messages": [{"role": "user", "content": _claim_message(entries, names)}]},
                config={"configurable": {
                    "project_id": project_id,
                    "project_state_single_writer": True,
                    "workbench_trace": True,
                }},
                multitask_strategy="enqueue",
            )
        except Exception:  # noqa: BLE001  # create may have succeeded despite a lost response
            for _, task, _ in entries:
                self._mark_triggered(task["run_id"], None)
            _LOGGER.warning("无法为项目 %s 创建领取 Run", project_id, exc_info=True)
            return False
        for _, task, _ in entries:
            self._mark_triggered(task["run_id"], claim.get("run_id") if isinstance(claim, dict) else None)
        return True

    async def poll_once(self, client) -> list[tuple[str, str]]:
        """Observe every conversation; return (thread_id, run_id) pairs it woke PM for."""
        if self.store is None:
            self.store = await get_store()
        woken = []
        offset = 0
        while True:
            bindings = await self.store.asearch(_THREADS, limit=100, offset=offset)
            for binding in bindings:
                thread_id = (binding.value or {}).get("thread_id")
                project_id = (binding.value or {}).get("project_id")
                if not isinstance(thread_id, str) or not thread_id or not isinstance(project_id, str) or not project_id:
                    continue
                values = await self._thread_values(client, thread_id)
                entries = await self._claimable(client, thread_id, values)
                if entries:
                    names = values.get("acp_task_names") or {}
                    if await self._wake(client, project_id, thread_id, entries, names):
                        woken.extend((thread_id, task["run_id"]) for _, task, _ in entries)
            if len(bindings) < 100:
                return woken
            offset += len(bindings)

    async def _thread_values(self, client, thread_id: str) -> dict:
        """Read the PM thread's tracked task state; unreadable threads contribute nothing."""
        try:
            snapshot = await client.threads.get_state(thread_id)
        except Exception:  # noqa: BLE001
            return {}
        return snapshot.get("values") or {}


async def watch_claims() -> None:
    """Observe project bindings for the process lifetime; transient failures log and continue."""
    client = get_client(url=None)
    watcher = ClaimWatcher()
    while True:
        try:
            await watcher.poll_once(client)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  # a failed pass must not end the watch
            _LOGGER.warning("领取观察轮询失败，下一轮继续", exc_info=True)
        await asyncio.sleep(_poll_seconds())
