"""独立审查闭环的确定性验证：登记、读取、返工、重审与三种事实的区分。

审查链路（begin_codex_review 派出 → wait_agent 领取 → record_codex_review
登记 → codex_review_status 读取 → 不通过时按固定指令返工 → 重审替换 latest）
用真实官方工具循环驱动（create_agent + ACPMiddleware/ProjectStateMiddleware），
SDK 响应与 Clef 判定使用替身；不依赖真实模型或 Codex 进程。
"""

import asyncio
import json
from unittest.mock import patch

import httpx
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.store.memory import InMemoryStore
from test_acp_middleware import client_fixture
from test_pm_memory_integration import ToolModel

from src.deep_agents.agents.pm_agent.discussion import DiscussionGate
from src.middlewares.acp import ACPMiddleware
from src.middlewares.execution.security_policy import (
    EnforcementCapability,
    EnforcementFact,
)
from src.middlewares.project_state import (
    ProjectSnapshot,
    ProjectStateMiddleware,
    ProjectStore,
    Task,
    TaskStatus,
)
from src.middlewares.project_state.commands import TransitionTaskCommand
from src.middlewares.project_state.folders import ProjectFolders
from src.middlewares.project_state.service import apply_project_command
from src.middlewares.project_state.workflow import ProjectWorkflow

_ENFORCEMENT = EnforcementFact(
    "test_sandbox", EnforcementCapability.ENFORCEABLE,
    frozenset({"worker.readonly"}), mechanism="test fixture",
)


def call(name, args, identity="call"):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": identity}]
    )


def _worker_ref(role, thread_id, session_id, cwd):
    """ACP 官方最终 checkpoint 里的会话引用，登记与独立性检查都依赖它。"""
    return {
        "project_id": "p", "task_id": "t", "thread_id": thread_id,
        "provider_id": "claude" if role == "developer" else "codex",
        "role": role, "session_id": session_id,
        "isolation": "native-readonly", "cwd": cwd,
    }


async def _discussion_pass(work, gate, cwd):
    """登记一轮已通过的商讨；Clef 判定用 mock transport 固定为 pass。"""
    await work.record_start("p", "t", "parent", "dev-agent", "disc-run",
                            phase="discussion", readonly_enforcement=_ENFORCEMENT)
    ref = await work.record_result("p", "parent", {
        "agent_id": "dev-agent", "run_id": "disc-run", "status": "success",
        "result": "同意按当前方案实现。", "worker_ref": _worker_ref("developer", "dev-agent", "dev-sess", cwd),
    })

    async def handler(request):
        return httpx.Response(200, json={"success": True, "result": {
            "answers": {"development_readiness": {"choice": "pass"}}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await gate.evaluate("p", "t", owner_thread="parent", worker_result_ref=ref,
                            pm_confirmation="方案一致", account_id="acct", token="secret",
                            client=client)


async def review_state(tmp_path, decision):
    """构建"开发已交付、商讨已通过、一份 decision 审查待登记"的完整状态。"""
    store = InMemoryStore()
    work = ProjectWorkflow(store)
    gate = DiscussionGate(store)
    folder = str(tmp_path)
    await ProjectFolders(store).bind("p", folder)
    brief = await work.artifacts.create("p", "t", "task_brief", "# 任务\n实现加法")
    task = Task("t", "计算器", TaskStatus.IN_PROGRESS, "developer",
                frozenset({"repo.read"}), frozenset(), ("加法正确",),
                artifact_refs=(brief["reference"],))
    await ProjectStore(store).save(ProjectSnapshot("p", "验证", (), (task,)))
    await _discussion_pass(work, gate, folder)
    await work.record_start("p", "t", "parent", "dev-agent", "dev-run")
    await work.record_result("p", "parent", {
        "agent_id": "dev-agent", "run_id": "dev-run", "status": "success",
        "result": "已实现 adder.add", "worker_ref": _worker_ref("developer", "dev-agent", "dev-sess", folder),
    })
    snapshot = await ProjectStore(store).load("p")
    snapshot = apply_project_command(snapshot, TransitionTaskCommand("p", "t", TaskStatus.DEV_COMPLETE))
    await ProjectStore(store).save(
        apply_project_command(snapshot, TransitionTaskCommand("p", "t", TaskStatus.REVIEW))
    )
    subject = await work.delivery_subject("p", "t")
    await work.record_start("p", "t", "parent", "rev-agent", "rev-run",
                            phase="review", readonly_enforcement=_ENFORCEMENT,
                            expected_review_subject=subject["subject"])
    return store, work, gate, subject, folder


def _review_client(subject, folder, decision="fail", run_id="rev-run", findings=None):
    """SDK 替身：审查 Run 成功，最终 checkpoint 携带 Codex 会话引用与结论。"""
    report = json.dumps({
        "decision": decision, "subject": subject["subject"],
        "summary": "实现与验收不符" if decision == "fail" else "实现符合验收",
        "findings": ["缺少加法用例"] if decision == "fail" else [],
    } if findings is None else findings, ensure_ascii=False)
    client = client_fixture()
    client.runs.list.return_value = [{"run_id": run_id, "status": "success"}]

    def history(*args, **kwargs):
        return [{
            "values": {
                "owner_thread_id": "parent",
                "worker_ref": _worker_ref("reviewer", "rev-agent", "rev-sess", folder),
                "messages": [{
                    "type": "ai", "content": report,
                    "additional_kwargs": {"acp_run_id": kwargs["metadata"]["run_id"]},
                }],
            },
            "next": [], "checkpoint": {"checkpoint_id": "final"},
        }]

    client.threads.get_history.side_effect = history
    return client, report


def test_claimed_review_run_registers_receipt_and_reads_back(tmp_path):
    """领取审查 Run 后自动登记结论，登记结果可按当前交付版本读取。"""
    async def scenario():
        store, work, _, subject, folder = await review_state(tmp_path, "fail")
        client, _ = _review_client(subject, folder, decision="fail")
        graph = create_agent(
            ToolModel(responses=[
                call("wait_agent", {"timeout_ms": 0}, "collect-review"),
                AIMessage(content="已领取审查结论"),
            ]),
            middleware=[
                ProjectStateMiddleware(workflow=work),
                ACPMiddleware(async_subagents=[{
                    "name": "reviewer", "description": "review",
                    "graph_id": "worker", "url": "http://localhost:1",
                }]),
            ],
            store=store,
        )
        with (
            patch("deepagents.middleware.async_subagents.get_client", return_value=client),
            patch("src.middlewares.acp.middleware.get_client", return_value=client),
        ):
            result = await graph.ainvoke(
                {"messages": [("user", "领取审查")],
                 "async_tasks": {"rev-agent": {
                     "task_id": "rev-agent", "agent_name": "reviewer",
                     "thread_id": "rev-agent", "run_id": "rev-run",
                     "status": "running", "created_at": "2026-11-14T00:00:00Z",
                 }}},
                {"configurable": {
                    "project_id": "p", "thread_id": "parent",
                    "project_state_single_writer": True,
                }},
            )
        payload = json.loads(next(
            m for m in result["messages"] if isinstance(m, ToolMessage)
        ).content)
        claimed = payload["completed"][0]
        assert claimed["codex_review"]["decision"] == "fail"
        assert claimed["artifact_ref"]

        receipt = await store.aget(
            ("deep_loopminder", "project_codex_reviews", "p", "t"), "latest")
        assert receipt is not None
        status = await work.codex_review_status("p", "t")
        assert status["status"] == "fail"
        assert status["summary"] == "实现与验收不符"
        assert status["findings"] == ["缺少加法用例"]
        assert status["review_run_id"] == "rev-run"

    asyncio.run(scenario())


def test_rework_instruction_requires_failed_review_and_carries_findings(tmp_path):
    """返工固定指令只在接受当前不通过审查时放行，并把发现交接给负责人。"""
    async def scenario():
        store, work, _, subject, folder = await review_state(tmp_path, "fail")
        # 与中间件相同的领取路径登记 fail 结论。
        client, report = _review_client(subject, folder, decision="fail")
        claim = await work.record_result("p", "parent", {
            "agent_id": "rev-agent", "run_id": "rev-run", "status": "success",
            "result": report,
            "worker_ref": _worker_ref("reviewer", "rev-agent", "rev-sess", folder),
        })
        receipt = await work.record_codex_review("p", "t", review_run_id="rev-run")
        assert receipt["decision"] == "fail" and claim

        rework_client = client_fixture()
        graph = create_agent(
            ToolModel(responses=[
                call("followup_task",
                     {"target": "dev-agent", "message": "按照已确认方案处理审查问题"},
                     "rework"),
                AIMessage(content="已派返工"),
            ]),
            middleware=[
                ProjectStateMiddleware(workflow=work, pm_discussion_gate=True),
                ACPMiddleware(async_subagents=[{
                    "name": "developer", "description": "dev",
                    "graph_id": "worker", "url": "http://localhost:1",
                }]),
            ],
            store=store,
        )
        with (
            patch("deepagents.middleware.async_subagents.get_client", return_value=rework_client),
            patch("src.middlewares.acp.middleware.get_client", return_value=rework_client),
        ):
            await graph.ainvoke(
                {"messages": [("user", "安排返工")],
                 "async_tasks": {"dev-agent": {
                     "task_id": "dev-agent", "agent_name": "developer",
                     "thread_id": "dev-agent", "run_id": "dev-run",
                     "status": "success", "created_at": "2026-11-14T00:00:00Z",
                 }}},
                {"configurable": {
                    "project_id": "p", "thread_id": "parent",
                    "project_state_single_writer": True,
                }},
            )
        envelope = json.loads(
            rework_client.runs.create.await_args.kwargs["input"]["messages"][0]["content"]
        )
        assert "独立审查的发现" in envelope["message"]
        assert "缺少加法用例" in envelope["message"]
        assert "按照已确认方案处理审查问题" not in envelope["message"]

    asyncio.run(scenario())


def test_rework_is_refused_without_a_current_failed_review(tmp_path):
    """当前交付没有有效的不通过审查时，返工固定指令必须被拒绝。"""
    async def scenario():
        store, work, _, subject, folder = await review_state(tmp_path, "pass")
        client, report = _review_client(subject, folder, decision="pass")
        await work.record_result("p", "parent", {
            "agent_id": "rev-agent", "run_id": "rev-run", "status": "success",
            "result": report, "worker_ref": _worker_ref("reviewer", "rev-agent", "rev-sess", folder),
        })
        await work.record_codex_review("p", "t", review_run_id="rev-run")
        assert (await work.codex_review_status("p", "t"))["status"] == "pass"

        graph = create_agent(
            ToolModel(responses=[
                call("followup_task",
                     {"target": "dev-agent", "message": "按照已确认方案处理审查问题"},
                     "rework"),
                AIMessage(content="停止"),
            ]),
            middleware=[
                ProjectStateMiddleware(workflow=work, pm_discussion_gate=True),
                ACPMiddleware(async_subagents=[{
                    "name": "developer", "description": "dev",
                    "graph_id": "worker", "url": "http://localhost:1",
                }]),
            ],
            store=store,
        )
        with (
            patch("deepagents.middleware.async_subagents.get_client", return_value=client),
            patch("src.middlewares.acp.middleware.get_client", return_value=client),
        ):
            result = await graph.ainvoke(
                {"messages": [("user", "尝试返工")],
                 "async_tasks": {"dev-agent": {
                     "task_id": "dev-agent", "agent_name": "developer",
                     "thread_id": "dev-agent", "run_id": "dev-run",
                     "status": "success", "created_at": "2026-11-14T00:00:00Z",
                 }}},
                {"configurable": {
                    "project_id": "p", "thread_id": "parent",
                    "project_state_single_writer": True,
                }},
            )
        rejected = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert rejected.status == "error"
        assert "current_failed_review_required" in rejected.content
        assert client.runs.create.await_count == 0

    asyncio.run(scenario())


def test_rework_invalidates_old_review_and_rereview_replaces_latest(tmp_path):
    """返工后旧审查失效；重审登记前为 newer_review_pending，登记后 latest 替换。"""
    async def scenario():
        store, work, _, subject, folder = await review_state(tmp_path, "fail")
        client, report = _review_client(subject, folder, decision="fail")
        await work.record_result("p", "parent", {
            "agent_id": "rev-agent", "run_id": "rev-run", "status": "success",
            "result": report, "worker_ref": _worker_ref("reviewer", "rev-agent", "rev-sess", folder),
        })
        await work.record_codex_review("p", "t", review_run_id="rev-run")
        assert (await work.codex_review_status("p", "t"))["status"] == "fail"

        # 返工：负责人重新交付，交付版本变化。
        await work.record_start("p", "t", "parent", "dev-agent", "dev-run-2")
        await work.record_result("p", "parent", {
            "agent_id": "dev-agent", "run_id": "dev-run-2", "status": "success",
            "result": "已补充加法用例", "worker_ref": _worker_ref("developer", "dev-agent", "dev-sess", folder),
        })
        stale = await work.codex_review_status("p", "t")
        assert stale["status"] == "unknown" and stale["reason"] == "review_subject_changed"

        # 重审：新 subject 登记后、结论领取前，旧 receipt 不能再当作当前审查。
        new_subject = await work.delivery_subject("p", "t")
        assert new_subject["subject"] != subject["subject"]
        await work.record_start("p", "t", "parent", "rev-agent-2", "rev-run-2",
                                phase="review", readonly_enforcement=_ENFORCEMENT,
                                expected_review_subject=new_subject["subject"])
        pending = await work.codex_review_status("p", "t")
        assert pending["status"] == "unknown" and pending["reason"] == "newer_review_pending"

        client2, report2 = _review_client(
            new_subject, folder, decision="pass", run_id="rev-run-2")
        await work.record_result("p", "parent", {
            "agent_id": "rev-agent-2", "run_id": "rev-run-2", "status": "success",
            "result": report2, "worker_ref": _worker_ref("reviewer", "rev-agent-2", "rev-sess-2", folder),
        })
        receipt = await work.record_codex_review("p", "t", review_run_id="rev-run-2")
        assert receipt["decision"] == "pass"
        current = await work.codex_review_status("p", "t")
        assert current["status"] == "pass" and current["review_run_id"] == "rev-run-2"

        # 旧 receipt 不可变保留，但不再是当前审查。
        old = await store.aget(
            ("deep_loopminder", "project_codex_reviews", "p", "t"),
            (await store.aget(("deep_loopminder", "project_codex_reviews", "p", "t"), "latest")).value["review_id"],
        )
        assert old.value["decision"] == "pass"

    asyncio.run(scenario())


def test_review_pass_validation_and_approval_stay_distinct(tmp_path):
    """审查通过、可信验收、人工批准是三件不同的事实，缺一不能推进。"""
    async def scenario():
        store, work, _, subject, folder = await review_state(tmp_path, "pass")
        client, report = _review_client(subject, folder, decision="pass")
        await work.record_result("p", "parent", {
            "agent_id": "rev-agent", "run_id": "rev-run", "status": "success",
            "result": report, "worker_ref": _worker_ref("reviewer", "rev-agent", "rev-sess", folder),
        })
        await work.record_codex_review("p", "t", review_run_id="rev-run")
        assert (await work.codex_review_status("p", "t"))["status"] == "pass"
        snapshot = await ProjectStore(store).load("p")
        # 审查通过≠可信验收：仍没有有效验收记录。
        facts = await work.facts(snapshot, "t", TaskStatus.PASSED)
        assert facts.validation_accepted is False and facts.human_approved is False

        # 通过审查的交付在 PM 侧推进 passed 仍被状态门禁拒绝（validation_required）。
        graph = create_agent(
            ToolModel(responses=[
                call("project_command",
                     {"action": "transition_task", "task_id": "t", "target_status": "passed"},
                     "pass"),
                AIMessage(content="需要宿主验收"),
            ]),
            middleware=[ProjectStateMiddleware(workflow=work, pm_discussion_gate=True)],
            store=store,
        )
        result = await graph.ainvoke(
            {"messages": [("user", "推进验收")]},
            {"configurable": {
                "project_id": "p", "thread_id": "parent",
                "project_state_single_writer": True,
            }},
        )
        rejected = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert rejected.status == "error" and "validation_required" in rejected.content

        # 宿主逐条核验写入可信验收；验收不等于批准。
        dev_binding = (await store.aget(("deep_loopminder", "project_execution_refs", "p"), "dev-run")).value
        rev_binding = (await store.aget(("deep_loopminder", "project_execution_refs", "p"), "rev-run")).value
        dev_ref, review_ref = dev_binding["result_ref"], rev_binding["result_ref"]
        await work._record_validation(
            "p", "t", validator_id="local-human",
            criteria={"加法正确": True},
            evidence_refs=(dev_ref, review_ref),
            required_run_ids=("dev-run", "rev-run"),
        )
        snapshot = await ProjectStore(store).load("p")
        facts = await work.facts(snapshot, "t", TaskStatus.PASSED)
        assert facts.validation_accepted is True and facts.human_approved is False

        # 人工批准是第三个独立记录；批准要求任务先处于已通过状态。
        snapshot = apply_project_command(
            snapshot, TransitionTaskCommand("p", "t", TaskStatus.PASSED,
                                            validation_accepted=True))
        await ProjectStore(store).save(snapshot)
        await work.record_approval("p", "t", actor_id="local-human",
                                   decision_ref="fixture://decision")
        snapshot = await ProjectStore(store).load("p")
        facts = await work.facts(snapshot, "t", TaskStatus.PASSED)
        assert facts.human_approved is True

    asyncio.run(scenario())
