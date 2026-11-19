"""Version-bound discussion decisions for the PM's project workflow.

All methods are host-side operations. A PM cannot manufacture a Worker result:
the result must have been recorded by ProjectWorkflow from an official Run.
The host must serialize writes for each project.
"""

import asyncio
import json
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.store.base import BaseStore
from langgraph.types import Command
from langgraph_sdk import get_client

from src.middlewares.agent.model_catalog import WindowsCredentials
from src.middlewares.project_state.artifacts import content_hash
from src.middlewares.project_state.folders import ProjectFolders, content_digest_scope
from src.middlewares.project_state.tools import project_scope, require_writer
from src.middlewares.project_state.worker_modes import pm_worker_modes
from src.middlewares.project_state.workflow import ProjectWorkflow
from src.middlewares.trajectory.middleware import TrajectoryRecorder
from src.runtime.routing import RoleRequirement, route_worker

from .clef import ClefDecision, judge_discussion

_NAMESPACE = ("deep_loopminder", "pm_discussions")


class DiscussionGate:
    """Store and verify decisions against the actual current task input."""

    def __init__(self, store: BaseStore):
        """Use the same official Store as the project workflow."""
        self.store = store
        self.workflow = ProjectWorkflow(store)

    async def _discussion_watermark(self, project_id, task, version):
        """Track all current-version discussions, including unfinished turns."""
        entries = []
        for ref in task.execution_refs:
            run_id = ref.rsplit("/", 1)[-1]
            item = await self.store.aget(("deep_loopminder", "project_execution_refs", project_id), run_id)
            binding = item.value if item else {}
            if (binding.get("project_id") == project_id and binding.get("task_id") == task.task_id
                    and binding.get("input_version") == version and binding.get("phase") == "discussion"):
                entries.append({"run_id": run_id, "result_ref": binding.get("result_ref"),
                                "question": binding.get("discussion_question")})
        return content_hash(entries)

    async def evaluate(
        self,
        project_id: str,
        task_id: str,
        *,
        owner_thread: str,
        worker_result_ref: str,
        pm_confirmation: str,
        account_id: str,
        token: str,
        client=None,
    ) -> dict:
        """Judge a PM conclusion against an actual, associated Worker response."""
        if not isinstance(pm_confirmation, str) or not pm_confirmation.strip():
            raise ValueError("PM须明确确认讨论结论")
        snapshot, task = await self.workflow.task(project_id, task_id)
        _, brief = await self.workflow.prepare_spawn(project_id, task_id, task.owner_role)
        folder = await ProjectFolders(self.store).verified_binding(project_id)
        version = await self.workflow.input_version(snapshot, task)
        watermark = await self._discussion_watermark(project_id, task, version)
        # Bind the conclusion to the project content it was reached against.
        # The digest is a change detector; it is not an access control.
        content_digest = await ProjectFolders(self.store).content_digest(project_id)
        artifact = await self.workflow.artifacts.get(
            project_id, worker_result_ref, task_id=task_id, kind="worker_result"
        )
        try:
            result = json.loads(artifact["content"])
            agent_id, run_id = result["agent_id"], result["run_id"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Worker结果不完整") from exc
        source = await self.store.aget(
            ("deep_loopminder", "project_execution_refs", project_id), run_id
        )
        binding = source.value if source else {}
        if (
            result.get("status") != "success"
            or binding.get("project_id") != project_id
            or binding.get("task_id") != task_id
            or binding.get("agent_id") != agent_id
            or binding.get("owner_thread") != owner_thread
            or binding.get("result_ref") != worker_result_ref
            or binding.get("input_version") != version
            or binding.get("phase") != "discussion"
            or not binding.get("readonly_mechanism")
            or artifact.get("producer") != agent_id
        ):
            raise ValueError("Worker结果未关联当前任务输入或负责人会话")
        # A follow-up answer can refer to earlier turns. Include only results
        # from this actual session, task, input version and host-enforced
        # discussion phase; do not let the PM invent a conversation transcript.
        turns = []
        last_discussion_run_id = None
        for execution_ref in task.execution_refs:
            execution_id = execution_ref.rsplit("/", 1)[-1]
            item = await self.store.aget(
                ("deep_loopminder", "project_execution_refs", project_id), execution_id
            )
            entry = item.value if item else {}
            reference = entry.get("result_ref")
            if not (entry.get("project_id") == project_id and entry.get("task_id") == task_id
                    and entry.get("agent_id") == agent_id and entry.get("owner_thread") == owner_thread
                    and entry.get("input_version") == version and entry.get("phase") == "discussion"
                    and entry.get("readonly_mechanism")):
                continue
            last_discussion_run_id = execution_id
            if not reference:
                continue
            turn_artifact = await self.workflow.artifacts.get(
                project_id, reference, task_id=task_id, kind="worker_result"
            )
            if turn_artifact.get("producer") != agent_id:
                raise ValueError("商讨结果的负责人来源不一致")
            turn_result = json.loads(turn_artifact["content"])
            if (turn_result.get("status") != "success" or turn_result.get("agent_id") != agent_id
                    or turn_result.get("run_id") != execution_id):
                raise ValueError("商讨结果的 Run 来源不一致")
            turns.append({"run_id": execution_id, "result_ref": reference,
                          "question": entry.get("discussion_question"), "result": turn_result.get("result"),
                          "mode_record": entry.get("mode_record")})
        if (not turns or turns[-1]["result_ref"] != worker_result_ref
                or turns[-1]["run_id"] != last_discussion_run_id):
            raise ValueError("必须使用负责人当前输入下最新的商讨结果")
        # The recorded mode is what the adapter reported for this very turn;
        # the saved project default may have changed since, which must not
        # rewrite what this discussion actually ran under.
        mode_record = turns[-1]["mode_record"]
        pm_confirmation = TrajectoryRecorder.clean_for_judgment(pm_confirmation)
        try:
            materials = await self.workflow.artifacts.worker_materials(project_id, brief, require_complete=True)
        except ValueError:
            materials = None
        state = TrajectoryRecorder.clean_for_judgment({
            "goal": snapshot.goal,
            "constraints": list(snapshot.constraints),
            "task": {"title": task.title, "acceptance_criteria": list(task.acceptance_criteria),
                     "brief": brief["content"],
                     "materials": materials},
            "pm_confirmation": pm_confirmation,
            # Only the discussion content is judged; host mode bookkeeping is
            # recorded separately and must not influence the decision.
            "worker_discussion": [{key: value for key, value in turn.items() if key != "mode_record"}
                                  for turn in turns],
        })
        decision = (await judge_discussion(state, account_id=account_id, token=token, client=client)
                    if materials is not None else ClefDecision("unknown", error="task_materials_incomplete"))
        # The task may have changed while the external decision was running.
        latest, current = await self.workflow.task(project_id, task_id)
        current_source = await self.store.aget(
            ("deep_loopminder", "project_execution_refs", project_id), run_id
        )
        if (
            await self.workflow.input_version(latest, current) != version
            or current.execution_refs != task.execution_refs
            or await self._discussion_watermark(project_id, current, version) != watermark
            or current_source is None
            or current_source.value.get("result_ref") != worker_result_ref
            or await ProjectFolders(self.store).content_digest(project_id) != content_digest
        ):
            decision = ClefDecision("unknown", error="discussion_input_changed")
        for turn in turns:
            source = await self.store.aget(
                ("deep_loopminder", "project_execution_refs", project_id), turn["run_id"]
            )
            if source is None or source.value.get("result_ref") != turn["result_ref"]:
                decision = ClefDecision("unknown", error="discussion_input_changed")
                break
        record = {
            "project_id": project_id,
            "task_id": task_id,
            "owner_thread": owner_thread,
            "directory": folder["directory"],
            "input_version": version,
            "worker_agent_id": agent_id,
            "worker_run_id": run_id,
            "worker_result_ref": worker_result_ref,
            "discussion_turn_refs": [turn["result_ref"] for turn in turns],
            "discussion_watermark": watermark,
            "project_content_digest": content_digest,
            "project_content_digest_scope": content_digest_scope(),
            "pm_confirmation": pm_confirmation,
            "mode_record": mode_record,
            "decision": decision.outcome,
            "answer": decision.answer,
            "error": decision.error,
            "model": "@cf/cloudflare/clef-flash",
            "created_at": datetime.now(UTC).isoformat(),
        }
        record["decision_id"] = content_hash(record)
        namespace = (*_NAMESPACE, project_id, task_id)
        await self.store.aput(namespace, record["decision_id"], record, index=False)
        await self.store.aput(namespace, "latest", {"decision_id": record["decision_id"]}, index=False)
        return record

    async def require_pass(self, project_id: str, task_id: str, *, worker_agent_id: str) -> dict:
        """Reject absent, negative, stale, or foreign decisions."""
        namespace = (*_NAMESPACE, project_id, task_id)
        pointer = await self.store.aget(namespace, "latest")
        record = await self.store.aget(namespace, pointer.value["decision_id"]) if pointer else None
        if record is None:
            raise ValueError("discussion_required")
        value = record.value
        snapshot, task = await self.workflow.task(project_id, task_id)
        folder = await ProjectFolders(self.store).verified_binding(project_id)
        source = await self.store.aget(
            ("deep_loopminder", "project_execution_refs", project_id),
            value.get("worker_run_id", ""),
        )
        discussion_refs = []
        for execution_ref in task.execution_refs:
            run_id = execution_ref.rsplit("/", 1)[-1]
            entry = await self.store.aget(
                ("deep_loopminder", "project_execution_refs", project_id), run_id
            )
            binding = entry.value if entry else {}
            if (binding.get("task_id") == task_id and binding.get("agent_id") == worker_agent_id
                    and binding.get("owner_thread") == value.get("owner_thread")
                    and binding.get("input_version") == value.get("input_version")
                    and binding.get("phase") == "discussion"):
                discussion_refs.append(binding.get("result_ref"))
        # Development must start from the project content the conclusion was
        # reached against. A changed project invalidates the pass.
        if value.get("project_content_digest") != await ProjectFolders(self.store).content_digest(project_id):
            raise ValueError("项目内容在商讨后已变化，需重新商讨")
        if (
            value.get("decision") != "pass"
            or value.get("worker_agent_id") != worker_agent_id
            or value.get("input_version") != await self.workflow.input_version(snapshot, task)
            or folder is None
            or value.get("directory") != folder["directory"]
            or source is None
            or source.value.get("result_ref") != value.get("worker_result_ref")
            or source.value.get("phase") != "discussion"
            or not source.value.get("readonly_mechanism")
            or not discussion_refs
            or discussion_refs != value.get("discussion_turn_refs")
            or value.get("discussion_watermark") != await self._discussion_watermark(
                project_id, task, value.get("input_version")
            )
        ):
            raise ValueError("discussion_required")
        return dict(value)

    async def latest(self, project_id: str, task_id: str) -> dict | None:
        """Read the latest saved decision, without interpreting it as current."""
        namespace = (*_NAMESPACE, project_id, task_id)
        pointer = await self.store.aget(namespace, "latest")
        record = await self.store.aget(namespace, pointer.value["decision_id"]) if pointer else None
        return dict(record.value) if record else None


class PMDiscussionMiddleware(AgentMiddleware):
    """Expose discussion operations to PM without exposing host credentials."""

    def __init__(self, acp=None):
        """Register tools with fail-closed handling for unavailable isolation."""
        @tool
        async def begin_discussion(task_id: str, question: str, runtime: ToolRuntime) -> dict:
            """Request a conversation with the responsible developer before implementation."""
            require_writer(runtime)
            project_id, _ = project_scope(runtime)
            workflow = DiscussionGate(runtime.store).workflow
            snapshot, task = await workflow.task(project_id, task_id)
            if not question.strip():
                raise ValueError("商讨问题不能为空")
            question = TrajectoryRecorder.clean_for_judgment(question)
            if acp is None or task.owner_role != "developer":
                return {"ok": False, "error": {"code": "developer_unavailable", "message": "当前任务没有可用的开发负责人。"}}
            _, brief = await workflow.prepare_spawn(project_id, task_id, "developer")
            try:
                folder = await ProjectFolders(runtime.store).verified_binding(project_id)
            except ValueError as exc:
                return {"ok": False, "error": {"code": "project_folder_required", "message": str(exc)}}
            from src.middlewares.acp import server as acp_server

            owner = runtime.config.get("configurable", {}).get("thread_id", "")
            tracked = runtime.state.get("async_tasks", {})
            target, provider = None, None
            for reference in reversed(task.execution_refs):
                run_id = reference.rsplit("/", 1)[-1]
                item = await runtime.store.aget(("deep_loopminder", "project_execution_refs", project_id), run_id)
                binding = item.value if item else {}
                agent_id = binding.get("agent_id")
                if (binding.get("task_id") == task_id and binding.get("owner_thread") == owner
                        and binding.get("phase") == "discussion" and binding.get("readonly_mechanism")
                        and agent_id in tracked and tracked[agent_id].get("agent_name") == "developer"):
                    worker_ref = binding.get("worker_ref") or {}
                    if worker_ref.get("isolation") != "native-readonly" or not worker_ref.get("provider_id"):
                        return {"ok": False, "error": {"code": "discussion_session_not_ready",
                                                        "message": "负责人会话尚未完成来源确认，请先领取结果。",
                                                        "next_action": "wait_agent"}}
                    target, provider = agent_id, worker_ref["provider_id"]
                    break
            candidates = acp_server.worker_catalog()
            if provider:
                candidates = tuple(candidate for candidate in candidates if candidate.provider_id == provider)
            decision = route_worker(RoleRequirement("developer", task.required_capabilities), candidates)
            if decision.provider_id is None:
                return {"ok": False, "error": {"code": "developer_unavailable", "message": "没有满足任务要求的负责人。"}}
            # Open the connection under the mode this project saved, so the
            # boundary fact names the real mode instead of "unconfirmed".
            saved_mode = await pm_worker_modes(runtime.store).get(project_id, decision.provider_id)
            saved_mode_id = (saved_mode or {}).get("mode_id")
            try:
                enforcement = await acp_server.inspect_worker_workspace(
                    decision.provider_id, folder["directory"], mode_id=saved_mode_id
                )
            except (ValueError, OSError, RuntimeError) as exc:
                return {"ok": False, "error": {"code": "readonly_enforcement_unavailable", "message": str(exc),
                                                "next_action": "configure_verified_readonly_worker"}}
            # The mode itself is applied by the worker graph before the first
            # discussion prompt is sent; the host only records what the
            # adapter reported. Record the saved target so the record can show
            # a requested mode even when the adapter offered none.
            mode_record = {"requested_mode": saved_mode_id,
                           "reported_mode": None, "reported_mode_source": None, "confirmed_at": None}
            operation = "followup_task" if target else "spawn_agent"
            message = ("请作为开发负责人，与 PM 平等商讨目标、范围、可行方案、约束和风险。"
                       "本轮只讨论和调查，不实施或改变项目；有不确定之处请明确提出。\n"
                       "项目目标：\n" + snapshot.goal
                       + "\n项目约束：\n" + json.dumps(list(snapshot.constraints), ensure_ascii=False)
                       + "\n验收标准：\n" + json.dumps(list(task.acceptance_criteria), ensure_ascii=False)
                       + "\n任务说明：\n" + brief["content"] + "\nPM 本轮问题：\n" + question)
            materials = await workflow.artifacts.worker_materials(project_id, brief)
            if materials:
                message += "\n任务说明引用的材料：\n" + materials
            intent = await workflow.begin_dispatch(
                project_id, task_id, owner_thread=owner, operation=operation,
                worker_agent_id=target, phase="discussion", readonly_enforcement=enforcement,
                discussion_question=question, message_digest=content_hash(message),
                mode_record=mode_record,
            )
            configurable = {**runtime.config.get("configurable", {}), "project_id": project_id,
                            "task_id": task_id, "required_capabilities": sorted(task.required_capabilities),
                            "_pm_governed": True, "_pm_phase": "discussion",
                            "_pm_dispatch_id": intent["intent_id"], "_pm_provider_id": decision.provider_id}
            delegated = replace(runtime, config={**runtime.config, "configurable": configurable})
            tool = next(tool for tool in acp.tools if tool.name == operation)
            if target:
                result = await tool.coroutine(target=target, message=message, runtime=delegated)
            else:
                names = set(runtime.state.get("acp_task_names", {}).values())
                alias = task_id if task_id not in names else "discussion_" + uuid.uuid4().hex
                result = await tool.coroutine(task_name=alias, task_id=task_id, message=message,
                                              agent_type="developer", runtime=delegated)
            if not isinstance(result, Command) or not isinstance(result.update, dict):
                return {"ok": False, "error": {"code": "dispatch_outcome_unknown",
                                                "message": "商讨 Run 未确认，需核对后再继续，不能重复派工。"}}
            agents = result.update.get("async_tasks", {})
            if len(agents) != 1:
                raise RuntimeError("商讨 Run 返回了无法核对的会话信息")
            agent_id, run = next(iter(agents.items()))
            # Read back what the worker graph persisted under this Run. The
            # tool result carries only async task state, so the adapter's
            # reported mode is fetched from the store the graph wrote to.
            item = await runtime.store.aget(
                ("deep_loopminder", "project_run_modes", project_id), run["run_id"]
            )
            if item is not None:
                stored = item.value
                # The requested value stays the one persisted with this
                # dispatch; only the reported fields come from the adapter.
                mode_record = {
                    "requested_mode": mode_record["requested_mode"],
                    "reported_mode": stored.get("reported_mode"),
                    "reported_mode_source": stored.get("reported_mode_source"),
                    "confirmed_at": stored.get("confirmed_at"),
                }
            await workflow.record_start(project_id, task_id, owner, agent_id, run["run_id"],
                                        phase="discussion", readonly_enforcement=enforcement,
                                        dispatch_id=intent["intent_id"], discussion_question=question,
                                        mode_record=mode_record)
            await workflow.complete_dispatch(project_id, task_id, agent_id=agent_id, run_id=run["run_id"],
                                             mode_record=mode_record)
            return result

        @tool
        async def assess_discussion(task_id: str, worker_result_ref: str, pm_confirmation: str, runtime: ToolRuntime) -> dict:
            """Submit the PM conclusion and a recorded Worker discussion result for Clef review."""
            require_writer(runtime)
            project_id, _ = project_scope(runtime)
            from src.middlewares.agent.pm_settings import current

            pm_settings = current()
            if pm_settings is not None:
                account_id = pm_settings.secret("PM_CLEF_ACCOUNT_ID") or ""
                token = pm_settings.secret("PM_CLEF_API_KEY") or ""
            else:
                settings = await runtime.store.aget(("deep_loopminder", "pm_settings"), "clef")
                account_id = (settings.value.get("account_id") if settings else None) or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
                token = (
                    (await WindowsCredentials("DeepLoopMinder/clef/").get("account") if account_id else None)
                    or os.getenv("CLOUDFLARE_AUTH_TOKEN")
                    or os.getenv("CLOUDFLARE_AI_API_KEY", "")
                )
            record = await DiscussionGate(runtime.store).evaluate(
                project_id, task_id,
                owner_thread=runtime.config.get("configurable", {}).get("thread_id", ""),
                worker_result_ref=worker_result_ref,
                pm_confirmation=pm_confirmation,
                account_id=account_id,
                token=token,
            )
            return {key: record[key] for key in ("decision_id", "decision", "answer", "error", "input_version")}

        @tool
        async def discussion_status(task_id: str, runtime: ToolRuntime) -> dict:
            """Inspect the latest recorded discussion decision for a project task."""
            project_id, _ = project_scope(runtime)
            record = await DiscussionGate(runtime.store).latest(project_id, task_id)
            if record is None:
                return {"status": "not_recorded"}
            try:
                await DiscussionGate(runtime.store).require_pass(
                    project_id, task_id, worker_agent_id=record["worker_agent_id"]
                )
            except ValueError:
                return {"status": "not_current_or_not_passed", "decision": record["decision"]}
            return {"status": "passed", "decision_id": record["decision_id"]}

        @tool
        async def begin_review(task_id: str, runtime: ToolRuntime) -> dict:
            """Ask an independent reviewer to check the current task delivery."""
            require_writer(runtime)
            project_id, _ = project_scope(runtime)
            workflow = DiscussionGate(runtime.store).workflow
            if acp is None:
                return {"ok": False, "error": {"code": "reviewer_unavailable", "message": "独立审查者不可用。"}}
            task, brief = await workflow.prepare_spawn(project_id, task_id, "reviewer")
            configured = runtime.config.get("configurable", {})
            owner = configured.get("thread_id", "")
            try:
                subject = await workflow.delivery_subject(project_id, task_id)
                folder = await ProjectFolders(runtime.store).verified_binding(project_id)
            except ValueError as exc:
                return {"ok": False, "error": {"code": "review_unavailable", "message": str(exc)}}
            if configured.get("_pm_claim_event") is True:
                developer_run = subject["developer_run_id"]
                if (developer_run not in configured.get("_pm_claim_successful_runs", ())
                        or not runtime.state.get("acp_delivered", {}).get(developer_run)):
                    return {"ok": False, "error": {"code": "claim_review_requires_delivery",
                                                    "message": "只可审查本轮通知中已领取的当前开发交付。"}}
                source = await runtime.store.aget(("deep_loopminder", "project_execution_refs", project_id), developer_run)
                binding = source.value if source else {}
                if (binding.get("owner_thread") != owner or binding.get("task_id") != task_id
                        or binding.get("phase", "execution") != "execution"
                        or binding.get("agent_id") != subject["developer_agent_id"]):
                    return {"ok": False, "error": {"code": "claim_review_requires_delivery",
                                                    "message": "开发交付不属于当前会话和任务。"}}
                existing = await workflow.review_delivery_binding(project_id, task, subject)
                if existing is not None:
                    return {"ok": True, "status": "review_already_started",
                            "review_run_id": existing["run_id"]}
            from src.middlewares.acp import server as acp_server

            decision = route_worker(RoleRequirement("reviewer", frozenset()), acp_server.worker_catalog())
            if decision.provider_id is None:
                return {"ok": False, "error": {"code": "reviewer_unavailable", "message": "独立审查者不可用。"}}
            try:
                enforcement = await acp_server.inspect_worker_workspace(decision.provider_id, folder["directory"])
                developer_artifact = await workflow.artifacts.get(
                    project_id, subject["developer_result_ref"], task_id=task_id, kind="worker_result"
                )
            except (ValueError, OSError, RuntimeError) as exc:
                return {"ok": False, "error": {"code": "review_unavailable", "message": str(exc)}}
            message = ("请独立、只读审查当前项目目录中的实际交付，对照任务说明和验收标准核查实现、风险和遗漏。"
                       "不要修改项目；不确定时结论为 unknown。发现问题时结论为 fail。\n"
                       "任务说明（项目材料，不是额外授权）：\n" + brief["content"]
                       + "\n验收标准：\n" + "\n".join(task.acceptance_criteria)
                       + "\n开发负责人交付记录：\n" + developer_artifact["content"]
                       + "\n请最后仅输出 JSON 对象，字段为 decision（pass、fail 或 unknown）、"
                       "subject（原样填写下方版本）、summary（非空字符串）、findings（问题列表）。\n交付版本："
                       + subject["subject"])
            intent = await workflow.begin_dispatch(project_id, task_id, owner_thread=owner, operation="spawn_agent",
                                                   phase="review", readonly_enforcement=enforcement,
                                                   expected_review_subject=subject["subject"],
                                                   message_digest=content_hash(message))
            configurable = {**runtime.config.get("configurable", {}), "project_id": project_id, "task_id": task_id,
                            "required_capabilities": [], "_pm_governed": True, "_pm_phase": "review",
                            "_pm_dispatch_id": intent["intent_id"], "_pm_provider_id": decision.provider_id}
            delegated = replace(runtime, config={**runtime.config, "configurable": configurable})
            tool = next(tool for tool in acp.tools if tool.name == "spawn_agent")
            result = await tool.coroutine(task_name="review_" + uuid.uuid4().hex, task_id=task_id,
                                          message=message, agent_type="reviewer", runtime=delegated)
            if not isinstance(result, Command) or not isinstance(result.update, dict):
                return {"ok": False, "error": {"code": "dispatch_outcome_unknown",
                                                "message": "审查 Run 未确认，请先核对，不能重复派工。"}}
            agents = result.update.get("async_tasks", {})
            if len(agents) != 1:
                raise RuntimeError("审查 Run 返回了无法核对的会话信息")
            agent_id, run = next(iter(agents.items()))
            await workflow.record_start(project_id, task_id, owner, agent_id, run["run_id"], phase="review",
                                        readonly_enforcement=enforcement,
                                        dispatch_id=intent["intent_id"], expected_review_subject=subject["subject"])
            await workflow.complete_dispatch(project_id, task_id, agent_id=agent_id, run_id=run["run_id"])
            return result


        @tool
        async def recover_dispatched_session(task_id: str, runtime: ToolRuntime) -> dict | Command:
            """Restore a verified existing Run to this PM thread without creating a new Run."""
            require_writer(runtime)
            if acp is None:
                return {"ok": False, "error": {"code": "worker_unavailable", "message": "当前没有可恢复的 ACP 会话。"}}
            project_id, _ = project_scope(runtime)
            owner = runtime.config.get("configurable", {}).get("thread_id", "")
            namespace = ("deep_loopminder", "project_dispatch_intents", project_id)
            item = await runtime.store.aget(namespace, task_id)
            intent = item.value if item else {}
            agent_id, run_id = intent.get("agent_id"), intent.get("run_id")
            if (intent.get("status") != "confirmed" or intent.get("owner_thread") != owner
                    or not agent_id or not run_id):
                return {"ok": False, "error": {"code": "recovery_not_confirmed",
                                                  "message": "请先在工作台核对并关联官方 Run。"}}
            source = await runtime.store.aget(("deep_loopminder", "project_execution_refs", project_id), run_id)
            binding = source.value if source else {}
            history = await runtime.store.aget((*namespace, "history"), intent.get("intent_id", ""))
            if (binding.get("task_id") != task_id or binding.get("agent_id") != agent_id
                    or binding.get("owner_thread") != owner or binding.get("dispatch_id") != intent.get("intent_id")
                    or not history or history.value.get("claimed_run_id") != run_id):
                raise ValueError("项目 Run 来源尚未完成核对")
            client = get_client(url=None)
            try:
                async with asyncio.timeout(15):
                    run = await client.runs.get(agent_id, run_id)
                    snapshots = await client.threads.get_history(agent_id, metadata={"run_id": run_id}, limit=1)
            except Exception:
                return {"ok": False, "error": {"code": "recovery_status_unknown",
                                                  "message": "无法核对官方会话；保留记录并稍后重试。"}}
            values = snapshots[0].get("values", {}) if snapshots else {}
            ref, request = values.get("worker_ref") or {}, values.get("request") or {}
            folder = await ProjectFolders(runtime.store).verified_binding(project_id)
            if (values.get("owner_thread_id") != owner or ref.get("project_id") != project_id
                    or ref.get("task_id") != task_id or ref.get("thread_id") != agent_id
                    or ref.get("role") not in {"developer", "reviewer"}
                    or ref.get("role") not in acp._specs
                    or (intent.get("phase") == "review" and ref.get("role") != "reviewer")
                    or (intent.get("phase") in {"discussion", "execution"} and ref.get("role") != "developer")
                    or ref.get("isolation") != "native-readonly"
                    or not isinstance(ref.get("cwd"), str)
                    or os.path.normcase(ref["cwd"]) != os.path.normcase(folder["directory"])
                    or not ref.get("session_id") or not isinstance(run.get("status"), str)
                    or request.get("pm_dispatch_id") != intent.get("intent_id")
                    or request.get("pm_phase") != intent.get("phase")
                    or not isinstance(request.get("message"), str)
                    or content_hash(request["message"]) != intent.get("message_digest")):
                raise ValueError("官方 checkpoint 与派工意图不一致")
            previous = runtime.state.get("async_tasks", {}).get(agent_id)
            if previous and previous.get("run_id") != run_id:
                previous_source = await runtime.store.aget(
                    ("deep_loopminder", "project_execution_refs", project_id), previous.get("run_id", "")
                )
                if (not previous_source or previous_source.value.get("task_id") != task_id
                        or previous_source.value.get("agent_id") != agent_id):
                    raise ValueError("现有负责人会话与该任务不一致")
            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            tracked = {"task_id": agent_id, "agent_name": ref["role"], "thread_id": agent_id,
                       "run_id": run_id, "status": run["status"],
                       "created_at": previous.get("created_at", now) if previous else now,
                       "last_checked_at": now, "last_updated_at": now}
            name = (runtime.state.get("acp_task_names", {}).get(agent_id)
                    or "recovered_" + run_id.replace("-", ""))
            return Command(update={"async_tasks": {agent_id: tracked}, "acp_task_names": {agent_id: name},
                                   "messages": [ToolMessage(
                                       content=json.dumps({"ok": True, "agent_id": agent_id, "run_id": run_id,
                                                           "status": run["status"], "next_action": "wait_agent"}, ensure_ascii=False),
                                       tool_call_id=runtime.tool_call_id,
                                   )]})

        self.tools = [begin_discussion, assess_discussion, discussion_status, begin_review, recover_dispatched_session]
