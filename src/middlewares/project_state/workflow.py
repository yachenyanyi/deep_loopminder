"""任务交付闭环：官方Run来源关联、结果制品与宿主可信验收/审批。"""

import json
from dataclasses import asdict

from langgraph.store.base import BaseStore

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
from .models import TaskStatus
from .persistence import ProjectStore
from .service import apply_project_command
from .tools import TransitionFacts
from .transitions import DependencyReadiness, ProjectStateError, dependency_readiness


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

    async def subject(self, snapshot, task):
        """状态推进不改变版本；新的运行、交付物或需求改变版本。"""
        for reference in task.artifact_refs:
            await self.artifacts.get(
                snapshot.project_id, reference, task_id=task.task_id
            )
        return content_hash(
            {
                "input": await self.input_version(snapshot, task),
                "executions": task.execution_refs,
                "artifacts": task.artifact_refs,
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
            raise ValueError("Worker角色必须与任务owner_role一致")
        await self.input_version(snapshot, task)
        brief = await self.artifacts.get(
            project_id, task.artifact_refs[0], task_id=task_id, kind="task_brief"
        )
        return task, brief

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

    async def record_start(self, project_id, task_id, owner_thread, agent_id, run_id):
        """关联官方工具创建的Run；重复相同关联不增加任务引用。"""
        snapshot, task = await self.task(project_id, task_id)
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
        }
        if previous and any(
            previous.value.get(key) != value[key]
            for key in value
            if key != "worker_context_version"
        ):
            raise ValueError("该Run已经关联另一任务或另一版输入")
        if not previous:
            await self.store.aput(namespace, run_id, value, index=False)
        # 派工本身就是分配动作；只在官方Run确认创建后推进，避免假进行中。
        updated = apply_project_command(
            snapshot, AttachTaskEvidenceCommand(project_id, task_id, (reference,), ())
        )
        for source, target in (
            (TaskStatus.BACKLOG, TaskStatus.READY),
            (TaskStatus.READY, TaskStatus.ASSIGNED),
            (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS),
        ):
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
            )
            if key in result
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

    async def record_validation(
        self,
        project_id,
        task_id,
        *,
        validator_id: str,
        criteria: dict[str, bool],
        evidence_refs: tuple[str, ...],
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
        input_version = await self.input_version(snapshot, task)
        # 每个子代理只取最新运行；追问或重试后，旧运行不能替代新结果。
        latest = {}
        for ref in task.execution_refs:
            run_id = ref.rsplit("/", 1)[-1]
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
        if not latest:
            raise ValueError("任务没有可验证的执行来源")
        subject = SubjectRef(
            f"{project_id}/{task_id}", await self.subject(snapshot, task)
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
            raise ValueError("当前版本没有有效验收，不能批准")
        validation = await self.store.aget(
            ("deep_loopminder", "project_validations", project_id), task_id
        )
        await self.store.aput(
            ("deep_loopminder", "project_approvals", project_id),
            task_id,
            {
                "schema_version": 1,
                "subject": await self.subject(snapshot, task),
                "validation_ref": validation.value["reference"],
                "actor_id": actor_id,
                "decision_ref": decision_ref,
                "approved": approved,
            },
            index=False,
        )

    async def facts(self, snapshot, task_id, target):
        """仅向状态策略返回当前版本的可信记录。"""
        task = next((task for task in snapshot.tasks if task.task_id == task_id), None)
        if task is None:
            raise ValueError("业务任务不存在，无法查询验收与批准")
        version = await self.subject(snapshot, task)
        validation = await self.store.aget(
            ("deep_loopminder", "project_validations", snapshot.project_id), task_id
        )
        accepted = bool(
            validation
            and validation.value["subject"] == version
            and validation.value.get("reference")
            == "project-validation://"
            + content_hash(
                {k: v for k, v in validation.value.items() if k != "reference"}
            )
            and set(validation.value["criteria"]) == set(task.acceptance_criteria)
            and all(value is True for value in validation.value["criteria"].values())
        )
        approval = await self.store.aget(
            ("deep_loopminder", "project_approvals", snapshot.project_id), task_id
        )
        approved = bool(
            accepted
            and approval
            and approval.value.get("approved") is True
            and approval.value["subject"] == version
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

    async def review_status(self, snapshot):
        """给PM提供真实门禁事实，不能从未验收推断宿主拒绝。"""
        statuses = []
        for task in snapshot.tasks:
            if task.status not in {
                TaskStatus.REVIEW,
                TaskStatus.TESTING,
                TaskStatus.PASSED,
                TaskStatus.WAITING_HUMAN,
            }:
                continue
            receipt = await self.store.aget(
                ("deep_loopminder", "project_validations", snapshot.project_id),
                task.task_id,
            )
            facts = await self.facts(snapshot, task.task_id, TaskStatus.PASSED)
            statuses.append(
                {
                    "task_id": task.task_id,
                    "validation_record_present": receipt is not None,
                    "validation_accepted_current": facts.validation_accepted,
                    "approval_accepted_current": facts.human_approved,
                    "validation_state": "当前版本验收通过"
                    if facts.validation_accepted
                    else (
                        "尚未记录验收，不代表宿主拒绝"
                        if receipt is None
                        else "已有记录但当前版本未获有效通过，需核对记录"
                    ),
                    "next_action": "按controls推进"
                    if facts.validation_accepted
                    else "请求宿主核验；可继续无依赖阻塞的任务，不修改真实前置依赖",
                }
            )
            if len(statuses) >= 30:
                break
        return statuses
