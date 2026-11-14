"""PM-specific authorization and mode rules for ACP Worker turns."""

from datetime import UTC, datetime

from langgraph.config import get_store

from src.middlewares.acp.control import UnsupportedControl
from src.middlewares.acp.pool import mode_can_write
from src.middlewares.project_state.artifacts import content_hash


class PMWorkerGovernance:
    """Apply PM business rules before a Worker receives a prompt."""

    def __init__(self, store_provider=get_store):
        """Use the runtime Store; tests may provide an isolated Store."""
        self.get_store = store_provider

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
