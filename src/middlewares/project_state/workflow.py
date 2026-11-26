"""任务交付闭环：官方Run来源关联、结果制品与宿主可信验收/审批。"""

import asyncio
import json
import uuid
from dataclasses import asdict

from langgraph.store.base import BaseStore
from langgraph_sdk import get_client

from src.middlewares.execution.security_policy import (
    EnforcementCapability,
    EnforcementFact,
)
from src.middlewares.execution.validation_policy import (
    EvidenceFact,
    ReviewFact,
    SubjectRef,
    ValidationStatus,
    validate_evidence,
    validate_independent_review,
)

from .artifacts import ProjectArtifacts, content_hash
from .commands import AttachTaskEvidenceCommand, TransitionTaskCommand
from .folders import ProjectFolders
from .models import TaskStatus
from .persistence import ProjectStore
from .scope import project_write_lock
from .service import apply_project_command
from .tools import TransitionFacts
from .transitions import DependencyReadiness, ProjectStateError, dependency_readiness


def parse_review_report(text):
    """Read a reviewer's structured verdict even when it is wrapped in prose.

    Reviewers reliably emit the required JSON object but often prefix it with a
    sentence ("I will only inspect ..."). Rejecting that on ``json.loads`` threw
    away a real verdict and downgraded the whole review to ``unknown``, so a
    completed fail became indistinguishable from no review at all. Only the
    object is parsed; nothing is inferred from the surrounding prose, and every
    field is still checked by the caller.
    """
    if not isinstance(text, str):
        raise TypeError("review result must be text")
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in review result")
    return json.loads(text[start : end + 1])


class ProjectWorkflow:
    """只保存业务关联与不可变证据，不维护另一份Run状态或调度队列。"""

    def __init__(self, store: BaseStore):
        """绑定共享Store与项目制品存储。"""
        self.store = store
        self.artifacts = ProjectArtifacts(store)

    async def task(self, project_id, task_id):
        """读取当前项目及指定业务任务。"""
        snapshot = await ProjectStore(self.store).load(project_id)
        if snapshot is None:
            raise ValueError("项目不存在")
        task = next((task for task in snapshot.tasks if task.task_id == task_id), None)
        if task is None:
            raise ValueError(
                "业务任务不存在，请先创建任务；spawn_agent.task_name必须使用业务task_id"
            )
        return snapshot, task

    async def input_version(self, snapshot, task):
        """将目标、约束、任务要求和TaskBrief绑定为执行输入版本。"""
        if not task.artifact_refs:
            raise ValueError("任务缺少TaskBrief")
        brief = await self.artifacts.get(
            snapshot.project_id,
            task.artifact_refs[0],
            task_id=task.task_id,
            kind="task_brief",
        )
        value = asdict(task)
        for key in ("status", "execution_refs", "artifact_refs"):
            value.pop(key)
        value["required_capabilities"] = sorted(value["required_capabilities"])
        value["dependencies"] = sorted(value["dependencies"])
        return content_hash(
            {
                "goal": snapshot.goal,
                "constraints": snapshot.constraints,
                "task": value,
                "brief": brief["reference"],
            }
        )

    async def prepare_spawn(self, project_id, task_id, role):
        """派工前验证状态、角色和实际可读的任务说明。"""
        snapshot, task = await self.task(project_id, task_id)
        if task.status not in {
            TaskStatus.BACKLOG,
            TaskStatus.READY,
            TaskStatus.ASSIGNED,
            TaskStatus.IN_PROGRESS,
            TaskStatus.REWORK,
            TaskStatus.REVIEW,
            TaskStatus.TESTING,
        }:
            raise ValueError("请先将任务推进到可执行状态，已结束任务不能再次派工")
        if dependency_readiness(snapshot, task_id) is not DependencyReadiness.READY:
            raise ProjectStateError(
                "dependencies_blocked",
                "任务的前置依赖尚未完成，不能派工或续问；请读取blocking_dependencies",
                "inspect_dependencies",
            )
        allowed_roles = {task.owner_role} if task.owner_role else set()
        if task.status in {TaskStatus.REVIEW, TaskStatus.TESTING}:
            allowed_roles.update({"reviewer", "tester"})
        if not task.owner_role or (role is not None and role not in allowed_roles):
            if role in {"reviewer", "tester"} and task.status not in {TaskStatus.REVIEW, TaskStatus.TESTING}:
                # The live run hit exactly this: the rejection named the role
                # rule, the agent could not tell that the missing step was a
                # status transition, and the review silently never happened.
                raise ValueError(
                    f"reviewer/tester 只能在 review 或 testing 状态派出，"
                    f"当前 {task.status.value}；请先用 transition_task 推进状态"
                )
            raise ValueError("Worker角色必须与任务owner_role一致")
        await self.input_version(snapshot, task)
        brief = await self.artifacts.get(
            project_id, task.artifact_refs[0], task_id=task_id, kind="task_brief"
        )
        return task, brief

    async def delivery_subject(self, project_id, task_id, *, allow_unreadable_directory=False):
        """Bind the latest developer result and current project files to review."""
        snapshot, task = await self.task(project_id, task_id)
        input_version = await self.input_version(snapshot, task)
        latest = None
        for reference in task.execution_refs:
            run_id = reference.rsplit("/", 1)[-1]
            item = await self.store.aget(("deep_loopminder", "project_execution_refs", project_id), run_id)
            binding = item.value if item else {}
            worker = binding.get("worker_ref") or {}
            if (binding.get("task_id") != task_id or binding.get("reference") != reference):
                raise ValueError("任务含无法验证的执行来源")
            if binding.get("phase", "execution") != "execution":
                continue
            if not worker.get("role") or not worker.get("session_id"):
                raise ValueError("执行会话来源尚不可验证")
            if worker.get("role") == "developer":
                latest = binding
        if (not latest or latest.get("input_version") != input_version or not latest.get("result_ref")):
            raise ValueError("当前版本缺少已完成的开发负责人交付")
        artifact = await self.artifacts.get(project_id, latest["result_ref"], task_id=task_id, kind="worker_result")
        result = json.loads(artifact["content"])
        if result.get("status") != "success" or result.get("run_id") != latest["run_id"]:
            raise ValueError("开发负责人最新 Run 未成功交付")
        files = await ProjectFolders(self.store).content_digest(project_id, allow_unreadable=allow_unreadable_directory)
        value = {"project_id": project_id, "task_id": task_id, "input_version": input_version,
                 "developer_agent_id": latest["agent_id"], "developer_run_id": latest["run_id"],
                 "developer_result_ref": latest["result_ref"], "project_content_digest": files}
        return {**value, "subject": content_hash(value), "developer_worker_ref": latest["worker_ref"]}

    async def review_delivery_binding(self, project_id, task, delivery, *, exact=True):
        """Find an existing review from the task's verified Run associations."""
        namespace = ("deep_loopminder", "project_execution_refs", project_id)
        for reference in reversed(task.execution_refs):
            item = await self.store.aget(namespace, reference.rsplit("/", 1)[-1])
            if item is None or item.value.get("phase") != "review":
                continue
            binding = item.value
            subject = binding.get("review_subject") or {}
            if (binding.get("task_id") == task.task_id and binding.get("reference") == reference
                    and subject.get("developer_run_id") == delivery["developer_run_id"]
                    and (not exact or subject.get("subject") == delivery["subject"])):
                return binding
        return None

    async def begin_dispatch(self, project_id, task_id, *, owner_thread, operation, decision_id=None,
                             worker_agent_id=None, phase="execution", readonly_enforcement=None,
                             message_digest=None, discussion_question=None, expected_review_subject=None,
                             mode_record=None, discussion_required: bool = True):
        """Persist a dispatch intent before contacting Agent Server.

        The caller must serialize project writers. A pending intent is deliberately
        not retried: the remote run may exist even when its response was lost.
        """
        # A discussion may run under a writable mode the user chose. What is
        # still required is a declared boundary: the phase must be bounded by
        # the adapter's mode and host checks, not left undefined.
        readonly_authorized = (readonly_enforcement is not None
                               and readonly_enforcement.capability
                               in {EnforcementCapability.ENFORCEABLE, EnforcementCapability.PARTIAL})
        if (type(discussion_required) is not bool
                or operation not in {"spawn_agent", "followup_task"} or not owner_thread
                or phase not in {"execution", "discussion", "review"}
                or not message_digest
                or (phase == "execution" and discussion_required and not decision_id)
                or (phase in {"discussion", "review"} and not readonly_authorized)
                or (phase == "discussion" and not discussion_question)
                or (phase == "review" and not expected_review_subject)
                or (operation == "followup_task" and not worker_agent_id)):
            raise ValueError("派工意图缺少可信授权信息")
        snapshot, task = await self.task(project_id, task_id)
        uncertain = await self.store.asearch(("deep_loopminder", "project_worker_quarantine", project_id), limit=100)
        if uncertain and (phase != "discussion" or any(
                not item.value.get("task_id") or item.value["task_id"] == task_id for item in uncertain)):
            raise ProjectStateError("worker_shutdown_unknown", "存在未确认停止的项目 Worker，不能继续派工。", "reconcile_worker_shutdown")
        namespace = ("deep_loopminder", "project_dispatch_intents", project_id)
        previous = await self.store.aget(namespace, task_id)
        if previous and previous.value.get("status") == "pending":
            raise ProjectStateError(
                "dispatch_outcome_unknown", "上次派工结果未确认，需核对官方 Run 后人工处理，不能重复派工。", "reconcile_dispatch"
            )
        if previous and previous.value.get("status") == "confirmed":
            old = previous.value
            agent_id, run_id = old.get("agent_id"), old.get("run_id")
            if not agent_id or not run_id:
                raise ProjectStateError("dispatch_outcome_unknown", "上次派工缺少官方 Run 来源。", "reconcile_dispatch")
            try:
                async with asyncio.timeout(15):
                    run = await get_client(url=None).runs.get(agent_id, run_id)
            except Exception as exc:
                # A missing dev registry entry is not evidence that the
                # original worker stopped or that its result was delivered.
                raise ProjectStateError("dispatch_status_unknown", "无法确认上一 Run 状态，暂不能重复派工。", "reconcile_dispatch") from exc
            if run.get("status") not in {"success", "error", "cancelled", "interrupted", "timeout"}:
                raise ProjectStateError("dispatch_still_running", "上一 Run 尚未结束，不能并发派工同一任务。", "wait_agent")
        value = {
            "project_id": project_id, "task_id": task_id, "owner_thread": owner_thread,
            "operation": operation, "decision_id": decision_id,
            "discussion_required": bool(discussion_required),
            "message_digest": message_digest,
            "phase": phase,
            "readonly_mechanism": readonly_enforcement.mechanism if readonly_enforcement else None,
            "readonly_capability": (readonly_enforcement.capability.value
                                    if readonly_enforcement else None),
            "readonly_gap": readonly_enforcement.gap if readonly_enforcement else None,
            "discussion_question": discussion_question,
            "expected_review_subject": expected_review_subject,
            "worker_agent_id": worker_agent_id,
            "mode_record": dict(mode_record) if mode_record else None,
            "input_version": await self.input_version(snapshot, task), "status": "pending",
            "intent_id": uuid.uuid4().hex,
        }
        await self.store.aput((*namespace, "history"), value["intent_id"], value, index=False)
        await self.store.aput(namespace, task_id, value, index=False)
        return value

    async def complete_dispatch(self, project_id, task_id, *, agent_id, run_id, mode_record=None):
        """Record the official Run only after its project association succeeds.

        The adapter-reported mode is known only after the worker graph ran, so
        it is filled in here; the requested target was already persisted with
        the dispatch intent and is never rewritten.
        """
        namespace = ("deep_loopminder", "project_dispatch_intents", project_id)
        item = await self.store.aget(namespace, task_id)
        if not item or item.value.get("status") != "pending" or not agent_id or not run_id:
            raise ValueError("没有待确认的派工意图")
        binding = await self.store.aget(("deep_loopminder", "project_execution_refs", project_id), run_id)
        if (not binding or binding.value.get("task_id") != task_id
                or binding.value.get("agent_id") != agent_id
                or (item.value.get("worker_agent_id") and item.value["worker_agent_id"] != agent_id)
                or binding.value.get("owner_thread") != item.value.get("owner_thread")
                or binding.value.get("dispatch_id") != item.value.get("intent_id")):
            raise ValueError("官方 Run 尚未关联当前任务")
        if binding.value.get("input_version") != item.value.get("input_version"):
            raise ValueError("官方 Run 输入与派工授权不一致")
        historical = await self.store.aget((*namespace, "history"), item.value["intent_id"])
        claimed = historical.value.get("claimed_run_id") if historical else None
        if claimed and claimed != run_id:
            raise ValueError("派工意图已绑定另一官方 Run")
        confirmed = {**item.value, "status": "confirmed", "agent_id": agent_id, "run_id": run_id}
        if claimed:
            confirmed["claimed_run_id"] = claimed
        if mode_record is not None:
            confirmed["mode_record"] = dict(mode_record)
            binding_item = await self.store.aget(
                ("deep_loopminder", "project_execution_refs", project_id), run_id
            )
            if binding_item is not None:
                await self.store.aput(
                    ("deep_loopminder", "project_execution_refs", project_id), run_id,
                    {**binding_item.value, "mode_record": dict(mode_record)}, index=False,
                )
        await self.store.aput((*namespace, "history"), item.value["intent_id"], confirmed, index=False)
        await self.store.aput(namespace, task_id, confirmed, index=False)

    async def attach(self, project_id, task_id, *, executions=(), artifacts=()):
        """在唯一项目写入线程附加已核验引用。"""
        snapshot, _ = await self.task(project_id, task_id)
        updated = apply_project_command(
            snapshot,
            AttachTaskEvidenceCommand(
                project_id, task_id, tuple(executions), tuple(artifacts)
            ),
        )
        await ProjectStore(self.store).save(updated)

    async def record_start(
        self, project_id, task_id, owner_thread, agent_id, run_id, *,
        phase="execution", readonly_enforcement: EnforcementFact | None = None,
        dispatch_id: str | None = None,
        expected_review_subject: str | None = None,
        discussion_question: str | None = None,
        mode_record: dict | None = None,
    ):
        """关联官方工具创建的Run；重复相同关联不增加任务引用。"""
        if phase not in {"execution", "discussion", "review"}:
            raise ValueError("未知的Worker阶段")
        if phase in {"discussion", "review"} and (
            readonly_enforcement is None
            or readonly_enforcement.capability
            not in {EnforcementCapability.ENFORCEABLE, EnforcementCapability.PARTIAL}
        ):
            raise ValueError("商讨和审查需要已声明的执行边界")
        snapshot, task = await self.task(project_id, task_id)
        review_subject = None
        if phase == "review":
            if task.status not in {TaskStatus.REVIEW, TaskStatus.TESTING}:
                raise ValueError("独立审查只能针对待审查或测试中的任务")
            review_subject = await self.delivery_subject(project_id, task_id)
            if not expected_review_subject or review_subject["subject"] != expected_review_subject:
                raise ValueError("提交审查前交付版本已变化")
        reference = f"langgraph-run://{agent_id}/{run_id}"
        namespace = ("deep_loopminder", "project_execution_refs", project_id)
        previous = await self.store.aget(namespace, run_id)
        value = {
            "schema_version": 1,
            "worker_context_version": 1,
            "project_id": project_id,
            "task_id": task_id,
            "owner_thread": owner_thread,
            "agent_id": agent_id,
            "run_id": run_id,
            "reference": reference,
            "input_version": await self.input_version(snapshot, task),
            "required_capabilities": sorted(task.required_capabilities),
            "phase": phase,
            "dispatch_id": dispatch_id,
            "readonly_mechanism": readonly_enforcement.mechanism if readonly_enforcement else None,
            "readonly_capability": (readonly_enforcement.capability.value
                                    if readonly_enforcement else None),
            "readonly_gap": readonly_enforcement.gap if readonly_enforcement else None,
            "review_subject": review_subject,
            "discussion_question": discussion_question,
            "mode_record": dict(mode_record) if mode_record else None,
        }
        def conflicts(key):
            """Report a real disagreement between the two records.

            The mode record grows over the Run's life: the dispatch intent only
            knows the requested target, while the binding later gains what the
            adapter reported. Per-field, an absent value on either side is
            "not yet known", not a disagreement; two different present values
            still conflict.
            """
            stored = previous.value.get(key, "execution" if key == "phase" else None)
            incoming = value[key]
            if key == "mode_record":
                if stored is None or incoming is None:
                    return False
                for field in ("requested_mode", "reported_mode",
                              "reported_mode_source", "confirmed_at"):
                    left, right = stored.get(field), incoming.get(field)
                    if left is not None and right is not None and left != right:
                        return True
                return False
            return stored != incoming

        if previous and any(
            conflicts(key) for key in value if key != "worker_context_version"
        ):
            raise ValueError("该Run已经关联另一任务或另一版输入")
        if previous:
            # The existing binding may already carry fields this record lacks
            # (the adapter's report is written after the dispatch). Keep them
            # rather than dropping them with the discarded value.
            stored_mode = previous.value.get("mode_record")
            if stored_mode:
                merged = {**(stored_mode or {})}
                merged.update({key: item for key, item in (value.get("mode_record") or {}).items()
                               if item is not None})
                if merged != stored_mode:
                    await self.store.aput(namespace, run_id,
                                          {**previous.value, "mode_record": merged}, index=False)
        if not previous:
            await self.store.aput(namespace, run_id, value, index=False)
        # 派工本身就是分配动作；只在官方Run确认创建后推进，避免假进行中。
        updated = apply_project_command(
            snapshot, AttachTaskEvidenceCommand(project_id, task_id, (reference,), ())
        )
        for source, target in (() if phase in {"discussion", "review"} else (
            (TaskStatus.BACKLOG, TaskStatus.READY),
            (TaskStatus.READY, TaskStatus.ASSIGNED),
            (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS),
        )):
            if task.status is source:
                updated = apply_project_command(
                    updated, TransitionTaskCommand(project_id, task_id, target)
                )
                task = next(item for item in updated.tasks if item.task_id == task_id)
        await ProjectStore(self.store).save(updated)

    async def record_result(self, project_id, owner_thread, result):
        """把官方最终checkpoint的正文保存为带来源的交付制品。"""
        namespace = ("deep_loopminder", "project_execution_refs", project_id)
        item = await self.store.aget(namespace, result["run_id"])
        if item is None:
            raise ValueError("交付Run未关联本项目任务，不能把未知结果写入任务")
        binding = item.value
        if (
            binding["agent_id"] != result["agent_id"]
            or binding["owner_thread"] != owner_thread
        ):
            raise ValueError("交付结果不属于当前PM线程")
        worker_ref = result.get("worker_ref")
        if worker_ref is not None and (
            not isinstance(worker_ref, dict)
            or worker_ref.get("project_id") != project_id
            or worker_ref.get("task_id") != binding["task_id"]
            or worker_ref.get("thread_id") != binding["agent_id"]
        ):
            raise ValueError("Worker会话与交付来源不一致")
        if worker_ref is not None:
            if binding.get("worker_ref") is not None and binding["worker_ref"] != worker_ref:
                raise ValueError("同一 Run 的 Worker 会话不能改变")
            binding = {**binding, "worker_ref": worker_ref}
        # 正文来自ACP官方最终checkpoint，不解析其中的通过声明。
        stable_result = {
            key: result[key]
            for key in (
                "agent_id",
                "run_id",
                "status",
                "result",
                "checkpoint",
                "error",
                "stop_reason",
                "context_usage",
                "compaction_visibility",
            )
            if key in result
            and not (key == "context_usage" and result[key] is None)
            and not (key == "compaction_visibility" and result[key] == "unknown")
        }
        artifact = await self.artifacts.create(
            project_id,
            binding["task_id"],
            "worker_result" if result["status"] == "success" else "worker_failure",
            json.dumps(stable_result, ensure_ascii=False, sort_keys=True),
            source_refs=(binding["reference"],),
            producer=binding["agent_id"],
        )
        if binding.get("result_ref") and binding["result_ref"] != artifact["reference"]:
            previous = await self.artifacts.get(project_id, binding["result_ref"])
            previous_result = json.loads(previous["content"])
            # 取消收尾与官方dev持久化有竞态：中断可能在恢复后呈现底层error。
            # 只接纳无交付正文的这两类失败观察，不能用它替换成功交付或绕过验收。
            failure_observation = (
                {previous_result.get("status"), stable_result.get("status")}
                == {"interrupted", "error"}
                and all(
                    not value.get(key)
                    for value in (previous_result, stable_result)
                    for key in ("result", "checkpoint", "stop_reason")
                )
                and all(
                    (value.get("error") or {}).get("code")
                    == {"interrupted": "run_interrupted", "error": "run_error"}[
                        value["status"]
                    ]
                    for value in (previous_result, stable_result)
                )
            )
            if not failure_observation:
                raise ValueError("同一最终Run不能返回不同交付内容")
            binding = {
                **binding,
                "previous_result_refs": list(
                    dict.fromkeys(
                        [
                            *binding.get("previous_result_refs", []),
                            binding["result_ref"],
                        ]
                    )
                ),
            }
        await self.store.aput(
            namespace,
            result["run_id"],
            {**binding, "result_ref": artifact["reference"]},
            index=False,
        )
        await self.attach(
            project_id,
            binding["task_id"],
            executions=(binding["reference"],),
            artifacts=(artifact["reference"],),
        )
        return artifact["reference"]

    async def _review_report(self, project_id, task_id, review_run_id, delivery):
        """Verify the linked official Run, independent Worker session and report."""
        item = await self.store.aget(("deep_loopminder", "project_execution_refs", project_id), review_run_id)
        binding = item.value if item else {}
        ref = binding.get("worker_ref") or {}
        if (binding.get("project_id") != project_id or binding.get("task_id") != task_id
                or binding.get("run_id") != review_run_id or binding.get("phase") != "review"
                or not binding.get("readonly_mechanism") or not ref.get("provider_id")
                or ref.get("project_id") != project_id or ref.get("task_id") != task_id
                or ref.get("thread_id") != binding.get("agent_id")
                or ref.get("role") != "reviewer" or not ref.get("session_id")
                or not binding.get("result_ref")):
            raise ValueError("缺少可验证的独立审查来源")
        developer = delivery["developer_worker_ref"]
        if ((binding.get("review_subject") or {}).get("subject") != delivery["subject"]
                or binding.get("agent_id") == delivery["developer_agent_id"]
                or (developer.get("provider_id"), developer.get("session_id"))
                == (ref.get("provider_id"), ref.get("session_id"))):
            raise ValueError("审查与开发会话不独立，或交付已发生变化")
        artifact = await self.artifacts.get(project_id, binding["result_ref"], task_id=task_id, kind="worker_result")
        result = json.loads(artifact["content"])
        if (not isinstance(result, dict) or result.get("status") != "success"
                or result.get("run_id") != review_run_id or result.get("agent_id") != binding["agent_id"]):
            raise ValueError("审查 Run 未成功完成或来源不一致")
        try:
            report = parse_review_report(result["result"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("独立审查未返回有效的结构化结论") from exc
        if (not isinstance(report, dict) or report.get("decision") not in {"pass", "fail", "unknown"}
                or report.get("subject") != delivery["subject"]
                or not isinstance(report.get("summary"), str) or not report["summary"].strip()
                or not isinstance(report.get("findings"), list)
                or (report.get("decision") == "pass" and bool(report["findings"]))):
            raise ValueError("审查结论或交付版本不完整")
        return binding, report

    async def record_independent_review(self, project_id, task_id, *, review_run_id):
        """Record an independent review from the official Run supplied by the host."""
        delivery = await self.delivery_subject(project_id, task_id)
        binding, report = await self._review_report(project_id, task_id, review_run_id, delivery)
        receipt = {"project_id": project_id, "task_id": task_id, "review_run_id": review_run_id,
                   "review_agent_id": binding["agent_id"], "review_result_ref": binding["result_ref"],
                   "review_provider_id": binding["worker_ref"]["provider_id"],
                   "review_session_id": binding["worker_ref"]["session_id"],
                   "subject": delivery["subject"], "decision": report["decision"],
                   "summary": report["summary"], "findings": report["findings"]}
        receipt["review_id"] = content_hash(receipt)
        namespace = ("deep_loopminder", "project_independent_reviews", project_id, task_id)
        await self.store.aput(namespace, receipt["review_id"], receipt, index=False)
        await self.store.aput(namespace, "latest", {"review_id": receipt["review_id"]}, index=False)
        return receipt

    async def independent_review_status(self, project_id, task_id, *, delivery=None):
        """Resolve the current independently sourced review, if still valid."""
        namespace = ("deep_loopminder", "project_independent_reviews", project_id, task_id)
        pointer = await self.store.aget(namespace, "latest")
        item = await self.store.aget(namespace, pointer.value["review_id"]) if pointer else None
        if item is None:
            return {"status": "unknown", "reason": "independent_review_missing"}
        value = dict(item.value)
        try:
            if value.get("review_id") != content_hash({key: val for key, val in value.items() if key != "review_id"}):
                raise ValueError("invalid review receipt")
            _, task = await self.task(project_id, task_id)
            latest_review_run = None
            for reference in task.execution_refs:
                run_id = reference.rsplit("/", 1)[-1]
                source = await self.store.aget(("deep_loopminder", "project_execution_refs", project_id), run_id)
                if source and source.value.get("phase") == "review":
                    latest_review_run = run_id
            if latest_review_run != value.get("review_run_id"):
                return {"status": "unknown", "reason": "newer_review_pending"}
            current = delivery if delivery is not None else await self.delivery_subject(project_id, task_id)
            if current["subject"] != value.get("subject"):
                return {"status": "unknown", "reason": "review_subject_changed"}
            binding, report = await self._review_report(project_id, task_id, value["review_run_id"], current)
            worker = binding["worker_ref"]
            if (binding["result_ref"] != value.get("review_result_ref")
                    or binding["agent_id"] != value.get("review_agent_id")
                    or worker["provider_id"] != value.get("review_provider_id")
                    or worker["session_id"] != value.get("review_session_id")
                    or any(report[key] != value.get(key) for key in ("decision", "summary", "findings"))):
                raise ValueError("invalid review result")
        except (ValueError, TypeError, KeyError):
            return {"status": "unknown", "reason": "review_subject_unavailable"}
        return {"status": value.get("decision", "unknown"), "review_id": value["review_id"],
                "summary": value.get("summary"), "findings": value.get("findings"), "review_run_id": value.get("review_run_id"),
                "developer_run_id": current["developer_run_id"]}

    async def record_validation(
        self, project_id, task_id, *, validator_id: str,
        criteria: dict[str, bool], evidence_refs: tuple[str, ...],
    ):
        """Serialize trusted validation with the same project writer lock."""
        async with project_write_lock(project_id):
            return await self._record_validation(project_id, task_id, validator_id=validator_id,
                                                 criteria=criteria, evidence_refs=evidence_refs)

    async def _record_validation(
        self,
        project_id,
        task_id,
        *,
        validator_id: str,
        criteria: dict[str, bool],
        evidence_refs: tuple[str, ...],
        required_run_ids: tuple[str, ...] | None = None,
        directory_digest: str | None = None,
        delivery_input_version: str | None = None,
    ):
        """宿主专用：独立核验后写入，未注册为模型工具，不能仅凭Worker自述。"""
        snapshot, task = await self.task(project_id, task_id)
        if task.status not in {TaskStatus.REVIEW, TaskStatus.TESTING}:
            raise ValueError("只能验收review/testing中的任务")
        if (
            not validator_id.strip()
            or set(criteria) != set(task.acceptance_criteria)
            or any(type(v) is not bool for v in criteria.values())
        ):
            raise ValueError("验收人及逐条验收结果必须完整")
        if not evidence_refs or any(
            ref not in task.artifact_refs for ref in evidence_refs
        ):
            raise ValueError("验收依据必须是本任务的真实交付制品")
        input_version = delivery_input_version or await self.input_version(snapshot, task)
        # 每个子代理只取最新运行；追问或重试后，旧运行不能替代新结果。
        latest = {}
        found_runs = set()
        for ref in task.execution_refs:
            run_id = ref.rsplit("/", 1)[-1]
            if required_run_ids is not None and run_id not in required_run_ids:
                continue
            item = await self.store.aget(
                ("deep_loopminder", "project_execution_refs", project_id), run_id
            )
            if (
                item is None
                or item.value["task_id"] != task_id
                or item.value["reference"] != ref
            ):
                raise ValueError("任务含未知执行来源")
            latest[item.value["agent_id"]] = item.value
            found_runs.add(run_id)
        if required_run_ids is not None and found_runs != set(required_run_ids):
            raise ValueError("验收缺少指定的开发或审查 Run")
        if not latest:
            raise ValueError("任务没有可验证的执行来源")
        subject = SubjectRef(
            f"{project_id}/{task_id}",
            content_hash({"input": input_version, "runs": sorted(found_runs), "evidence": evidence_refs}),
        )
        for binding in latest.values():
            if (
                binding["input_version"] != input_version
                or binding.get("result_ref") not in evidence_refs
            ):
                raise ValueError("验收缺少当前输入下最新Run的交付物")
            artifact = await self.artifacts.get(
                project_id, binding["result_ref"], task_id=task_id, kind="worker_result"
            )
            fact = EvidenceFact(subject, all(criteria.values()), artifact["reference"])
            review = ReviewFact(
                subject,
                binding["agent_id"],
                validator_id,
                True,
                artifact["reference"],
            )
            if (
                validate_evidence(current_subject=subject, evidence=fact).status
                is ValidationStatus.UNAVAILABLE
                or validate_independent_review(
                    current_subject=subject, review=review, required=True
                ).status
                is not ValidationStatus.PASS
            ):
                raise ValueError("验收未通过或验收人与执行者不独立")
        receipt = {
            "schema_version": 1,
            "task_id": task_id,
            "subject": subject.version,
            "validator_id": validator_id,
            "criteria": criteria,
            "evidence_refs": list(evidence_refs),
        }
        if directory_digest is not None:
            receipt["directory_digest"] = directory_digest
        receipt["reference"] = "project-validation://" + content_hash(receipt)
        previous = await self.store.aget(
            ("deep_loopminder", "project_validations", project_id), task_id
        )
        if previous is None or previous.value["reference"] != receipt["reference"]:
            # 新验收结论先撤销旧批准，避免失败后再次通过时复活历史批准。
            # 相同记录的重复投递保持幂等，不要求人工重复批准。
            await self.store.adelete(
                ("deep_loopminder", "project_approvals", project_id), task_id
            )
        await self.store.aput(
            ("deep_loopminder", "project_validations", project_id),
            receipt["reference"],
            receipt,
            index=False,
        )
        await self.store.aput(
            ("deep_loopminder", "project_validations", project_id),
            task_id,
            receipt,
            index=False,
        )
        return receipt["reference"]

    async def record_approval(
        self, project_id, task_id, *, actor_id: str, decision_ref: str, approved: bool = True,
    ):
        """Serialize trusted human approval with project mutations."""
        async with project_write_lock(project_id):
            return await self._record_approval(project_id, task_id, actor_id=actor_id,
                                               decision_ref=decision_ref, approved=approved)

    async def _record_approval(
        self,
        project_id,
        task_id,
        *,
        actor_id: str,
        decision_ref: str,
        approved: bool = True,
    ):
        """宿主专用：由认证的人工决定入口调用，不推断模型或Worker文字中的批准。"""
        snapshot, task = await self.task(project_id, task_id)
        if (
            task.status
            not in {TaskStatus.PASSED, TaskStatus.WAITING_HUMAN, TaskStatus.APPROVED}
            or not actor_id.strip()
            or not decision_ref.strip()
        ):
            raise ValueError("人工批准需要已通过的任务、可信身份和决定引用")
        facts = await self.facts(snapshot, task_id, TaskStatus.PASSED)
        if not facts.validation_accepted:
            raise ValueError("没有有效的人工验收记录，不能批准")
        validation = await self.store.aget(
            ("deep_loopminder", "project_validations", project_id), task_id
        )
        await self.store.aput(
            ("deep_loopminder", "project_approvals", project_id),
            task_id,
            {
                "schema_version": 1,
                "validation_ref": validation.value["reference"],
                "actor_id": actor_id,
                "decision_ref": decision_ref,
                "approved": approved,
            },
            index=False,
        )

    async def facts(self, snapshot, task_id, target):
        """读取人工验收及针对该验收记录作出的批准。"""
        task = next((task for task in snapshot.tasks if task.task_id == task_id), None)
        if task is None:
            raise ValueError("业务任务不存在，无法查询验收与批准")
        validation = await self.store.aget(
            ("deep_loopminder", "project_validations", snapshot.project_id), task_id
        )
        accepted = bool(
            validation
            and validation.value.get("task_id") == task_id
            and validation.value.get("reference")
            == "project-validation://"
            + content_hash(
                {k: v for k, v in validation.value.items() if k != "reference"}
            )
            and validation.value["criteria"]
            and all(value is True for value in validation.value["criteria"].values())
        )
        approval = await self.store.aget(
            ("deep_loopminder", "project_approvals", snapshot.project_id), task_id
        )
        approved = bool(
            accepted
            and approval
            and approval.value.get("approved") is True
            and approval.value["validation_ref"] == validation.value["reference"]
        )
        return TransitionFacts(validation_accepted=accepted, human_approved=approved)

    async def invalidate(self, project_id, task_id):
        """返工时撤销当前指针，保留不可变验收历史。"""
        await self.store.adelete(
            ("deep_loopminder", "project_validations", project_id), task_id
        )
        await self.store.adelete(
            ("deep_loopminder", "project_approvals", project_id), task_id
        )

    async def review_status(self, snapshot, *, include_all_tasks=False):
        """给PM提供真实门禁事实，不能从未验收推断宿主拒绝。"""
        statuses = []
        for task in snapshot.tasks:
            if not include_all_tasks and task.status not in {
                TaskStatus.REVIEW,
                TaskStatus.TESTING,
                TaskStatus.PASSED,
                TaskStatus.WAITING_HUMAN,
                TaskStatus.APPROVED,
                TaskStatus.DONE,
            }:
                continue
            receipt = await self.store.aget(
                ("deep_loopminder", "project_validations", snapshot.project_id),
                task.task_id,
            )
            facts = await self.facts(snapshot, task.task_id, TaskStatus.PASSED)
            approval = await self.store.aget(
                ("deep_loopminder", "project_approvals", snapshot.project_id), task.task_id
            )
            approval_current = bool(
                approval and receipt
                and approval.value.get("validation_ref") == receipt.value.get("reference")
            )
            approval_state = ("not_recorded" if approval is None else "stale" if not approval_current
                              else "rejected" if approval.value.get("approved") is False
                              else "approved" if facts.human_approved else "unknown")
            statuses.append(
                {
                    "task_id": task.task_id,
                    "validation_record_present": receipt is not None,
                    "validated_criteria": receipt.value["criteria"] if receipt else None,
                    "validation_accepted_current": facts.validation_accepted,
                    "approval_accepted_current": facts.human_approved,
                    "approval_state": approval_state,
                    "validation_state": "已记录验收通过"
                    if facts.validation_accepted
                    else (
                        "尚未记录验收，不代表宿主拒绝"
                        if receipt is None
                        else "已有验收记录，尚未通过"
                    ),
                    "next_action": "按controls推进"
                    if facts.validation_accepted
                    else "请求宿主核验；可继续无依赖阻塞的任务，不修改真实前置依赖",
                }
            )
            if not include_all_tasks and len(statuses) >= 30:
                break
        return statuses
