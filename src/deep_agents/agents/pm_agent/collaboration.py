"""PM-specific authorization and mode rules for ACP Worker turns."""

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from langgraph.config import get_store

from src.middlewares.acp.control import UnsupportedControl
from src.middlewares.acp.pool import mode_can_write
from src.middlewares.project_state.artifacts import content_hash
from src.middlewares.project_state.folders import ProjectFolders
from src.middlewares.project_state.scope import project_write_lock


def _job_directory():
    base = os.getenv("LOOPMINDER_DATA_DIR") or os.getenv("LOCALAPPDATA")
    if not base:
        raise ValueError("未配置本机应用数据目录")
    return (Path(base) / "DeepLoopMinder" / "worker-jobs").resolve()


def job_observation(record):
    """Describe the last trusted Job observation without inferring live state."""
    try:
        path = Path(record["job_status"])
        execution_id = record["execution_id"]
        if (not path.is_absolute() or path.parent.resolve() != _job_directory()
                or path.name != execution_id + ".json"
                or len(execution_id) != 32
                or any(char not in "0123456789abcdef" for char in execution_id)):
            return {"state": "unknown", "reason": "监管记录路径或执行身份无效。"}
        with path.open(encoding="utf-8") as stream:
            status = json.load(stream)
        if not isinstance(status, dict) or status.get("execution_id") != execution_id:
            return {"state": "unknown", "reason": "监管记录与当前执行身份不匹配。"}
        state = status.get("state")
        reasons = {
            "starting": "启动记录尚未确认进程已分配到 Job，不能推断是否已经停止。",
            "assigned": "进程已分配到 Job，但尚无整个进程组已退出的证明。",
            "running": "最后记录为运行中；此记录不能证明监管进程当前仍在运行。",
            "stop_unknown": "监管过程失败，尚未确认整个进程组已退出。",
            "stopped_verified": "监管进程已确认 Job 内活动进程数为零。",
        }
        if state not in reasons:
            return {"state": "unknown", "reason": "监管记录包含无法识别的状态。"}
        if state == "stop_unknown":
            phases = {"job_setup": "创建进程组", "witness_start": "启动核验进程", "adapter_start": "启动适配器",
                      "job_assignment": "分配进程组", "status_write": "保存监管状态", "adapter_resume": "恢复适配器",
                      "adapter_wait": "等待适配器", "job_termination": "终止进程组", "job_drain": "确认进程组退出"}
            phase = phases.get(status.get("stage"))
            if phase:
                reasons[state] = f"{phase}时监管失败，尚未确认整个进程组已退出。"
        return {"state": state, "reason": reasons[state]}
    except FileNotFoundError:
        return {"state": "unknown", "reason": "监管状态文件不存在，无法证明进程组已停止。"}
    except (OSError, ValueError, TypeError, KeyError):
        return {"state": "unknown", "reason": "监管状态文件无法读取或格式无效。"}


def observed_job_state(record):
    """Read this execution's last observation; it is not a live process query."""
    return job_observation(record)["state"]


def verified_job_stop(record):
    """Accept only this execution's explicitly verified Job shutdown record."""
    return observed_job_state(record) == "stopped_verified"


async def reconcile_verified_jobs(store, project_id, *, locked=False):
    """Remove only records backed by their own confirmed Job shutdown."""
    namespace = ("deep_loopminder", "project_worker_quarantine", project_id)
    async def reconcile():
        entries = await store.asearch(namespace, limit=100)
        for entry in entries:
            value = entry.value
            ref = value.get("worker_ref") if isinstance(value, dict) else None
            if (isinstance(ref, dict) and value.get("project_id") == project_id
                    and ref.get("project_id") == project_id and ref.get("thread_id") == entry.key
                    and verified_job_stop(value)):
                await store.adelete(namespace, entry.key)
    if locked:
        await reconcile()
    else:
        async with project_write_lock(project_id):
            await reconcile()


class PMWorkerGovernance:
    """Apply PM business rules before a Worker receives a prompt."""

    def __init__(self, store_provider=get_store):
        """Use the runtime Store; tests may provide an isolated Store."""
        self.get_store = store_provider

    async def begin_execution(self, ref, request, config, ticket):
        """Persist the authorized execution identity before starting its process."""
        project_id = ref.project_id
        namespace = ("deep_loopminder", "project_worker_quarantine", project_id)
        directory = _job_directory()
        directory.mkdir(parents=True, exist_ok=True)
        execution_id = uuid.uuid4().hex
        status_path = directory / (execution_id + ".json")
        if status_path.exists():
            raise RuntimeError("Worker 状态路径已存在")
        await reconcile_verified_jobs(self.get_store(), project_id)
        async with project_write_lock(project_id):
            await self.authorize_execution(request, ref, config)
            if await self.get_store().asearch(namespace, limit=1):
                raise PermissionError("项目中已有运行中或未确认停止的 Worker")
            await ProjectFolders(self.get_store()).content_digest(project_id)
            run_id = str(config.get("metadata", {}).get("run_id", ""))
            if not run_id:
                raise PermissionError("开发请求缺少官方 Run 标识")
            await self.claim_intent(project_id, ref.task_id, request["pm_dispatch_id"], run_id)
            record = {"project_id": project_id, "task_id": ref.task_id,
                      "owner_thread_id": request["owner_thread_id"], "worker_ref": ref.to_dict(),
                      "intent_id": request["pm_dispatch_id"], "official_run_id": run_id,
                      "execution_id": execution_id, "job_status": str(status_path),
                      "status": "shutdown_unconfirmed", "ticket": ticket}
            await self.get_store().aput(namespace, ref.thread_id, record, index=False)
        return record

    async def finish_execution(self, ref, record, error=None):
        """Clear only the matching execution after verified Job shutdown."""
        namespace = ("deep_loopminder", "project_worker_quarantine", ref.project_id)
        async with project_write_lock(ref.project_id):
            item = await self.get_store().aget(namespace, ref.thread_id)
            if item is None or item.value.get("execution_id") != record["execution_id"]:
                return
            if error is None and verified_job_stop(item.value):
                await self.get_store().adelete(namespace, ref.thread_id)
            else:
                value = {**item.value, "status": "shutdown_unconfirmed"}
                if error is not None:
                    value["reason"] = str(error)[:500]
                await self.get_store().aput(namespace, ref.thread_id, value, index=False)

    async def claim_intent(self, project_id: str, task_id: str, dispatch_id: str, run_id: str):
        """把官方 Run 写回派工意图：历史记录写回 + 指针一致时同步 current。

        只读与执行派工共用同一写回语义，避免两处副本漂移；调用方已持项目写锁。
        """
        namespace = ("deep_loopminder", "project_dispatch_intents", project_id)
        item = await self.get_store().aget((*namespace, "history"), dispatch_id)
        if item is None:
            raise PermissionError("派工意图不存在，无法关联官方 Run")
        claimed = {**item.value, "claimed_run_id": run_id}
        await self.get_store().aput((*namespace, "history"), dispatch_id, claimed, index=False)
        current = await self.get_store().aget(namespace, task_id)
        if current and current.value.get("intent_id") == dispatch_id:
            await self.get_store().aput(namespace, task_id, claimed, index=False)

    async def readonly_mode(self, project_id, provider_id):
        """Read the saved target so the boundary fact names a real mode."""
        from src.middlewares.project_state.worker_modes import pm_worker_modes

        try:
            saved = await pm_worker_modes(self.get_store()).get(project_id, provider_id)
        except ValueError:
            return None
        return (saved or {}).get("mode_id")

    async def authorize_execution(self, request, ref, config):
        """Check the current decision and exact authorized dispatch."""
        from .discussion import DiscussionGate

        decision = await DiscussionGate(self.get_store()).require_pass(
            ref.project_id, ref.task_id, worker_agent_id=ref.thread_id
        )
        intent_id = request.get("pm_dispatch_id")
        if not isinstance(intent_id, str) or not intent_id:
            raise PermissionError("开发请求缺少持久化派工意图")
        intent = await self.get_store().aget(
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

    async def authorize_readonly(self, request, project_id, task_id, worker_id, config):
        """Check the current task version and authorized dispatch."""
        from src.middlewares.project_state.workflow import ProjectWorkflow

        intent_id = request.get("pm_dispatch_id")
        if not isinstance(intent_id, str) or not intent_id:
            raise PermissionError("只读请求缺少持久化意图")
        item = await self.get_store().aget(("deep_loopminder", "project_dispatch_intents", project_id, "history"), intent_id)
        value = item.value if item else {}
        run_id = str(config.get("metadata", {}).get("run_id", ""))
        workflow = ProjectWorkflow(self.get_store())
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

    async def apply_execution_mode(self, worker, ref):
        """Leave a confirmed read-only mode before sending development work."""
        try:
            mode_snapshot = worker.mode_state(ref.session_id)
        except Exception:
            mode_snapshot = {}
        current_id = (mode_snapshot or {}).get("current")
        if current_id is not None and not mode_can_write(ref.provider_id, current_id):
            offered = [item["id"] for item in (mode_snapshot or {}).get("modes", [])
                       if isinstance(item, dict) and item.get("id")]
            writable = [item for item in offered if mode_can_write(ref.provider_id, item)]
            if not writable:
                raise PermissionError(
                    f"负责人会话停留在只读模式 {current_id}，适配器未提供可写模式；未发送开发内容。"
                )
            target = "default" if "default" in writable else writable[0]
            applied = await worker.apply_mode(ref, target)
            if not applied.get("matched"):
                raise PermissionError(
                    f"执行模式切换未获适配器确认：请求 {target}，适配器报告 {applied.get('reported_mode')}。"
                )

    async def apply_discussion_mode(self, worker, ref, request, config):
        """Switch to the saved mode and return only what the adapter reported."""
        if not request.get("pm_governed") or request.get("pm_phase") != "discussion":
            return None
        requested = await self.readonly_mode(ref.project_id, ref.provider_id)
        # 需求默认商讨模式 plan：仅在适配器确实提供时使用；用户决定
        # （2026-11-14）默认 plan 不可用时不静默继续——明确报错，让用户
        # 在工作台为该项目选择可用模式后重新商讨。用户明确保存的目标
        # 模式不可用同样失败，不静默降级。
        default_attempt = not requested
        if default_attempt:
            requested = "plan"
        try:
            applied = await worker.apply_mode(ref, requested)
        except UnsupportedControl:
            if default_attempt:
                raise PermissionError(
                    "默认商讨模式 plan 不被该适配器提供；未发送商讨内容。"
                    "请让用户在工作台的商讨模式设置中为该项目选择适配器提供的"
                    "可用模式（可写模式会标注说明）后重新商讨。"
                )
            raise
        record = {"requested_mode": requested, "reported_mode": applied["reported_mode"],
                  "reported_mode_source": applied["reported_mode_source"],
                  "confirmed_at": applied["confirmed_at"]}
        if default_attempt:
            record["requested_mode_source"] = "default_plan"
        if not applied["matched"]:
            raise PermissionError(
                f"目标模式 {requested}，适配器报告 {applied['reported_mode']}；未发送商讨内容。"
            )
        run_id = str(config.get("metadata", {}).get("run_id", ""))
        if run_id:
            await self.get_store().aput(
                ("deep_loopminder", "project_run_modes", ref.project_id), run_id,
                {**record, "run_id": run_id, "provider_id": ref.provider_id,
                 "session_id": ref.session_id, "recorded_at": datetime.now(UTC).isoformat()},
                index=False,
            )
        return record
