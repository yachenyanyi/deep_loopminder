"""项目交付闭环的确定性边界：真实官方工具循环，SDK响应使用明确替身。"""

import asyncio
import json
from unittest.mock import patch

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from test_acp_middleware import client_fixture
from test_pm_memory_integration import ToolModel

from src.middlewares.acp import ACPMiddleware
from src.middlewares.project_state import (
    ProjectSnapshot,
    ProjectStateMiddleware,
    ProjectStore,
    Task,
    TaskStatus,
)
from src.middlewares.project_state.commands import ReplaceTaskBriefCommand
from src.middlewares.project_state.service import apply_project_command
from src.middlewares.project_state.workflow import ProjectWorkflow


def call(name, args, identity="call"):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": identity}]
    )


async def fixture(status=TaskStatus.IN_PROGRESS):
    store = InMemoryStore()
    work = ProjectWorkflow(store)
    brief = await work.artifacts.create(
        "p", "t", "task_brief", "# 任务\n检查实际代码依据"
    )
    task = Task(
        "t",
        "检查",
        status,
        "reviewer",
        frozenset({"repo.read"}),
        frozenset(),
        ("真实交付",),
        artifact_refs=(brief["reference"],),
    )
    await ProjectStore(store).save(ProjectSnapshot("p", "验证", (), (task,)))
    return store, work, task


@pytest.mark.parametrize(
    "status,target,code",
    [
        (TaskStatus.TESTING, "passed", "validation_required"),
        (TaskStatus.PASSED, "approved", "approval_required"),
    ],
)
def test_missing_trusted_receipt_reaches_hitl_without_bypassing_gate(
    tmp_path, status, target, code
):
    """真实状态工具拒绝后可继续到官方人工中断，不生成框架首错或伪造验收。"""
    from src.middlewares.approval.human_decision import (
        HumanDecisionRequestMiddleware,
        human_decision_hitl_middleware,
    )
    from src.middlewares.trajectory import TrajectoryMiddleware, TrajectoryRecorder

    async def scenario():
        store, work, _ = await fixture(status)
        graph = create_agent(
            ToolModel(
                responses=[
                    call(
                        "project_command",
                        {
                            "action": "transition_task",
                            "task_id": "t",
                            "target_status": target,
                        },
                        "transition",
                    ),
                    call(
                        "request_human_decision",
                        {
                            "question": "请宿主核验当前任务证据并补齐可信记录",
                            "context": "项目p，任务t，不能凭Worker自述通过",
                        },
                        "decision",
                    ),
                ]
            ),
            middleware=[
                TrajectoryMiddleware(TrajectoryRecorder(tmp_path), stop_on_error=True),
                HumanDecisionRequestMiddleware(),
                human_decision_hitl_middleware(),
                ProjectStateMiddleware(workflow=work),
            ],
            store=store,
            checkpointer=InMemorySaver(),
        )
        result = await graph.ainvoke(
            {"messages": [("user", "核对并推进")]},
            {
                "configurable": {
                    "project_id": "p",
                    "thread_id": "parent",
                    "project_state_single_writer": True,
                },
            },
        )
        rejected = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        error = json.loads(rejected.content)["error"]
        assert (
            error["code"] == code and error["next_action"] == "request_human_decision"
        )
        assert rejected.additional_kwargs["recoverable_business_rejection"] is True
        assert result["__interrupt__"]
        assert not (tmp_path / "first-error.json").exists()
        assert (await ProjectStore(store).load("p")).tasks[0].status == status
        assert (
            await store.asearch(("deep_loopminder", "project_validations", "p")) == []
        )
        assert await store.asearch(("deep_loopminder", "project_approvals", "p")) == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "bad", ["C:/missing/brief.md", "project-artifact://" + "a" * 64]
)
def test_pm_task_creation_rejects_unresolved_brief(bad):
    async def scenario():
        store, work, _ = await fixture()
        model = ToolModel(
            responses=[
                call(
                    "project_command",
                    {
                        "action": "create_task",
                        "task": {
                            "task_id": "bad",
                            "title": "bad",
                            "task_brief_ref": bad,
                            "acceptance_criteria": ["x"],
                        },
                    },
                ),
                AIMessage(content="blocked"),
            ]
        )
        graph = create_agent(
            model, middleware=[ProjectStateMiddleware(workflow=work)], store=store
        )
        result = await graph.ainvoke(
            {"messages": [("user", "创建")]},
            {"configurable": {"project_id": "p", "project_state_single_writer": True}},
        )
        assert (
            next(m for m in result["messages"] if isinstance(m, ToolMessage)).status
            == "error"
        )
        assert len((await ProjectStore(store).load("p")).tasks) == 1

    asyncio.run(scenario())


def test_artifacts_are_immutable_and_project_task_scoped():
    async def scenario():
        store, work, task = await fixture()
        ref = task.artifact_refs[0]
        assert (
            await work.artifacts.create(
                "p", "t", "task_brief", "# 任务\n检查实际代码依据"
            )
        )["reference"] == ref
        for project, task_id in (("foreign", "t"), ("p", "other")):
            with pytest.raises(ValueError):
                await work.artifacts.get(project, ref, task_id=task_id)
        item = await store.aget(
            ("deep_loopminder", "project_artifacts", "p"),
            ref.removeprefix("project-artifact://"),
        )
        await store.aput(
            item.namespace, item.key, {**item.value, "content": "tampered"}
        )
        with pytest.raises(ValueError, match="内容"):
            await work.artifacts.get("p", ref)

    asyncio.run(scenario())


def test_real_acp_tools_bind_task_and_persist_idempotent_delivery():
    async def scenario():
        store, work, task = await fixture()
        client = client_fixture()
        client.runs.list.return_value = [
            {"run_id": "run-1", "status": "success", "created_at": "2026-10-15"}
        ]
        model = ToolModel(
            responses=[
                call("spawn_agent", {"task_name": "t", "message": "执行任务"}, "spawn"),
                AIMessage(content="started"),
                call("wait_agent", {"timeout_ms": 0}, "wait"),
                AIMessage(content="collected"),
            ]
        )
        graph = create_agent(
            model,
            middleware=[
                ProjectStateMiddleware(workflow=work),
                ACPMiddleware(
                    async_subagents=[
                        {
                            "name": "reviewer",
                            "description": "review",
                            "graph_id": "worker",
                            "url": "http://localhost:1",
                        }
                    ]
                ),
            ],
            store=store,
            checkpointer=InMemorySaver(),
        )
        config = {
            "configurable": {
                "project_id": "p",
                "thread_id": "parent",
                "project_state_single_writer": True,
                "task_id": "wrong-host-id",
                "required_capabilities": ["wrong"],
            }
        }
        with (
            patch(
                "deepagents.middleware.async_subagents.get_client", return_value=client
            ),
            patch("src.middlewares.acp.middleware.get_client", return_value=client),
        ):
            await graph.ainvoke({"messages": [("user", "派工")]}, config)
            result = await graph.ainvoke({"messages": [("user", "收取")]}, config)
        request = json.loads(
            client.runs.create.await_args_list[0].kwargs["input"]["messages"][0][
                "content"
            ]
        )
        assert request["task_id"] == "t" and request["required_capabilities"] == [
            "repo.read"
        ]
        assert "检查实际代码依据" in request["message"]
        current = (await ProjectStore(store).load("p")).tasks[0]
        assert current.execution_refs == ("langgraph-run://child/run-1",)
        assert (
            current.status == TaskStatus.IN_PROGRESS and len(current.artifact_refs) == 2
        )
        output = next(
            json.loads(m.content)
            for m in result["messages"]
            if isinstance(m, ToolMessage) and m.tool_call_id == "wait"
        )
        completed = output["completed"][0]
        completed["last_checked_at"] = "different observation timestamp"
        await work.record_result("p", "parent", completed)
        assert (await ProjectStore(store).load("p")).tasks[
            0
        ].artifact_refs == current.artifact_refs

    asyncio.run(scenario())


async def delivered_fixture():
    store, work, task = await fixture(TaskStatus.REVIEW)
    await work.record_start("p", "t", "parent", "worker", "run-1")
    reference = await work.record_result(
        "p",
        "parent",
        {
            "agent_id": "worker",
            "run_id": "run-1",
            "status": "success",
            "result": "真实报告",
        },
    )
    return store, work, task, reference


def test_trusted_validation_and_separate_approval_unlock_official_state_loop():
    async def scenario():
        store, work, _, ref = await delivered_fixture()
        config = {
            "configurable": {
                "project_id": "p",
                "thread_id": "parent",
                "project_state_single_writer": True,
            }
        }

        def graph(status):
            return create_agent(
                ToolModel(
                    responses=[
                        call(
                            "project_command",
                            {
                                "action": "transition_task",
                                "task_id": "t",
                                "target_status": status,
                            },
                        ),
                        AIMessage(content="end"),
                    ]
                ),
                middleware=[ProjectStateMiddleware(workflow=work)],
                store=store,
            )

        first = await graph("passed").ainvoke({"messages": [("user", "验收")]}, config)
        assert (
            next(m for m in first["messages"] if isinstance(m, ToolMessage)).status
            == "error"
        )
        with pytest.raises(ValueError, match="独立"):
            await work.record_validation(
                "p",
                "t",
                validator_id="worker",
                criteria={"真实交付": True},
                evidence_refs=(ref,),
            )
        await work.record_validation(
            "p",
            "t",
            validator_id="trusted-host",
            criteria={"真实交付": True},
            evidence_refs=(ref,),
        )
        await graph("passed").ainvoke({"messages": [("user", "验收")]}, config)
        denied = await graph("approved").ainvoke(
            {"messages": [("user", "批准")]}, config
        )
        assert (
            next(m for m in denied["messages"] if isinstance(m, ToolMessage)).status
            == "error"
        )
        await work.record_approval(
            "p",
            "t",
            actor_id="test-human",
            decision_ref="test-fixture://explicit-decision",
        )
        await graph("approved").ainvoke({"messages": [("user", "批准")]}, config)
        await graph("done").ainvoke({"messages": [("user", "收口")]}, config)
        assert (await ProjectStore(store).load("p")).tasks[0].status == TaskStatus.DONE
        tools = ProjectStateMiddleware(workflow=work).tools
        assert all(
            "record_validation" not in tool.name and "record_approval" not in tool.name
            for tool in tools
        )

    asyncio.run(scenario())


def test_new_input_or_new_run_invalidates_previous_validation():
    async def scenario():
        store, work, _, ref = await delivered_fixture()
        await work.record_validation(
            "p",
            "t",
            validator_id="host",
            criteria={"真实交付": True},
            evidence_refs=(ref,),
        )
        snapshot = await ProjectStore(store).load("p")
        assert (await work.facts(snapshot, "t", TaskStatus.PASSED)).validation_accepted
        await work.record_start("p", "t", "parent", "worker", "run-2")
        current = await ProjectStore(store).load("p")
        assert not (
            await work.facts(current, "t", TaskStatus.PASSED)
        ).validation_accepted
        with pytest.raises(ValueError, match="最新Run"):
            await work.record_validation(
                "p",
                "t",
                validator_id="host",
                criteria={"真实交付": True},
                evidence_refs=(ref,),
            )
        brief = await work.artifacts.create(
            "p", "t", "task_brief", "# 新任务说明\n修订要求"
        )
        revised = apply_project_command(
            current, ReplaceTaskBriefCommand("p", "t", brief["reference"])
        )
        await ProjectStore(store).save(revised)
        assert not (
            await work.facts(revised, "t", TaskStatus.PASSED)
        ).validation_accepted

    asyncio.run(scenario())


def test_wrong_role_is_rejected_before_external_spawn():
    async def scenario():
        _, work, _ = await fixture()
        with pytest.raises(ValueError, match="角色"):
            await work.prepare_spawn("p", "t", "developer")

    asyncio.run(scenario())


def test_invalid_spawn_schema_stays_recoverable_before_business_binding():
    async def scenario():
        store, work, _ = await fixture()
        client = client_fixture()
        graph = create_agent(
            ToolModel(
                responses=[
                    call("spawn_agent", {"task_name": "t"}),
                    AIMessage(content="修正参数"),
                ]
            ),
            middleware=[
                ProjectStateMiddleware(workflow=work),
                ACPMiddleware(
                    async_subagents=[
                        {
                            "name": "reviewer",
                            "description": "read",
                            "graph_id": "worker",
                            "url": "http://localhost:1",
                        }
                    ]
                ),
            ],
            store=store,
        )
        with (
            patch(
                "deepagents.middleware.async_subagents.get_client", return_value=client
            ),
            patch("src.middlewares.acp.middleware.get_client", return_value=client),
        ):
            result = await graph.ainvoke(
                {"messages": [("user", "派工")]},
                {
                    "configurable": {
                        "project_id": "p",
                        "thread_id": "parent",
                        "project_state_single_writer": True,
                    }
                },
            )
        assert (
            next(m for m in result["messages"] if isinstance(m, ToolMessage)).status
            == "error"
        )
        client.runs.create.assert_not_awaited()

    asyncio.run(scenario())


def test_invalid_existing_brief_can_be_repaired_through_valid_artifact():
    async def scenario():
        store, work, task = await fixture()
        snapshot = await ProjectStore(store).load("p")
        from dataclasses import replace

        await ProjectStore(store).save(
            replace(snapshot, tasks=(replace(task, artifact_refs=("C:/missing.md",)),))
        )
        reference = task.artifact_refs[0]
        graph = create_agent(
            ToolModel(
                responses=[
                    call(
                        "project_command",
                        {
                            "action": "update_task_brief",
                            "task_id": "t",
                            "task_brief_ref": reference,
                        },
                    ),
                    AIMessage(content="已修复"),
                ]
            ),
            middleware=[ProjectStateMiddleware(workflow=work)],
            store=store,
        )
        await graph.ainvoke(
            {"messages": [("user", "修复引用")]},
            {"configurable": {"project_id": "p", "project_state_single_writer": True}},
        )
        _, brief = await work.prepare_spawn("p", "t", "reviewer")
        assert brief["reference"] == reference

    asyncio.run(scenario())


def test_done_rechecks_current_evidence_after_goal_changes():
    async def scenario():
        from dataclasses import replace

        store, work, _, ref = await delivered_fixture()
        await work.record_validation(
            "p",
            "t",
            validator_id="host",
            criteria={"真实交付": True},
            evidence_refs=(ref,),
        )
        snapshot = await ProjectStore(store).load("p")
        await ProjectStore(store).save(
            replace(
                snapshot, tasks=(replace(snapshot.tasks[0], status=TaskStatus.PASSED),)
            )
        )
        await work.record_approval(
            "p", "t", actor_id="test-human", decision_ref="fixture://decision"
        )
        current = await ProjectStore(store).load("p")
        await ProjectStore(store).save(
            replace(
                current,
                goal="目标已变化",
                tasks=(replace(current.tasks[0], status=TaskStatus.APPROVED),),
            )
        )
        graph = create_agent(
            ToolModel(
                responses=[
                    call(
                        "project_command",
                        {
                            "action": "transition_task",
                            "task_id": "t",
                            "target_status": "done",
                        },
                    ),
                    AIMessage(content="需要重新核验"),
                ]
            ),
            middleware=[ProjectStateMiddleware(workflow=work)],
            store=store,
        )
        result = await graph.ainvoke(
            {"messages": [("user", "收口")]},
            {"configurable": {"project_id": "p", "project_state_single_writer": True}},
        )
        assert (
            next(m for m in result["messages"] if isinstance(m, ToolMessage)).status
            == "error"
        )
        assert (await ProjectStore(store).load("p")).tasks[
            0
        ].status == TaskStatus.APPROVED

    asyncio.run(scenario())


def test_new_negative_validation_revokes_old_positive_verdict():
    async def scenario():
        store, work, _, ref = await delivered_fixture()
        await work.record_validation(
            "p",
            "t",
            validator_id="host",
            criteria={"真实交付": True},
            evidence_refs=(ref,),
        )
        await work.record_validation(
            "p",
            "t",
            validator_id="host",
            criteria={"真实交付": False},
            evidence_refs=(ref,),
        )
        snapshot = await ProjectStore(store).load("p")
        assert not (
            await work.facts(snapshot, "t", TaskStatus.PASSED)
        ).validation_accepted

    asyncio.run(scenario())


@pytest.mark.parametrize("increase_capabilities", [False, True, "unchanged"])
def test_followup_uses_current_brief_and_rejects_increased_requirements(
    increase_capabilities,
):
    async def scenario():
        from dataclasses import replace

        store, work, _ = await fixture()
        note = await work.artifacts.create("p", "t", "note", "本轮新增证据正文")
        client = client_fixture()
        model = ToolModel(
            responses=[
                call("spawn_agent", {"task_name": "t", "message": "首次"}, "spawn"),
                AIMessage(content="started"),
                call(
                    "followup_task",
                    {"target": "t", "message": "继续，参考" + note["reference"]},
                    "follow",
                ),
                AIMessage(content="end"),
            ]
        )
        graph = create_agent(
            model,
            middleware=[
                ProjectStateMiddleware(workflow=work),
                ACPMiddleware(
                    async_subagents=[
                        {
                            "name": "reviewer",
                            "description": "review",
                            "graph_id": "worker",
                            "url": "http://localhost:1",
                        }
                    ]
                ),
            ],
            store=store,
            checkpointer=InMemorySaver(),
        )
        config = {
            "configurable": {
                "project_id": "p",
                "thread_id": "parent",
                "project_state_single_writer": True,
            }
        }
        with (
            patch(
                "deepagents.middleware.async_subagents.get_client", return_value=client
            ),
            patch("src.middlewares.acp.middleware.get_client", return_value=client),
        ):
            await graph.ainvoke({"messages": [("user", "派工")]}, config)
            brief = await work.artifacts.create(
                "p", "t", "task_brief", "更新后的任务说明"
            )
            snapshot = await ProjectStore(store).load("p")
            updated = apply_project_command(
                snapshot, ReplaceTaskBriefCommand("p", "t", brief["reference"])
            )
            if increase_capabilities:
                updated = replace(
                    updated,
                    tasks=(
                        replace(
                            updated.tasks[0],
                            required_capabilities=frozenset(
                                {"repo.read", "repo.write"}
                            ),
                        ),
                    ),
                )
            if increase_capabilities != "unchanged":
                await ProjectStore(store).save(updated)
            result = await graph.ainvoke({"messages": [("user", "续问")]}, config)
        response = next(
            m
            for m in result["messages"]
            if isinstance(m, ToolMessage) and m.tool_call_id == "follow"
        )
        if increase_capabilities is True:
            assert response.status == "error" and "能力" in response.content
            assert client.runs.create.await_count == 1
        else:
            request = json.loads(
                client.runs.create.await_args_list[-1].kwargs["input"]["messages"][0][
                    "content"
                ]
            )
            assert "本轮新增证据正文" in request["message"]
            assert request["message"].endswith(
                "原生工具失败由你自行判断与恢复，不代表宿主要求逐条执行或新增20–30秒限时。\n"
            )
            if increase_capabilities == "unchanged":
                assert "继续" in request["message"]
                assert "# 任务" not in request["message"]
            else:
                assert "更新后的任务说明" in request["message"]
            assert (await ProjectStore(store).load("p")).tasks[0].execution_refs == (
                "langgraph-run://child/run-1",
                "langgraph-run://child/run-2",
            )

    asyncio.run(scenario())


def test_mixed_project_write_and_followup_can_be_retried_without_partial_effects(
    tmp_path,
):
    """重放本次混批形态，拒绝后下一轮串行修改可以继续，续问不应偷偷启动。"""
    from src.middlewares.trajectory import TrajectoryMiddleware, TrajectoryRecorder

    async def scenario():
        store, work, _ = await fixture()
        mixed = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "project_command",
                    "args": {"action": "update_project", "goal": "不应生效"},
                    "id": "write",
                },
                {
                    "name": "followup_task",
                    "args": {"target": "absent", "message": "不应发出"},
                    "id": "follow",
                },
            ],
        )
        graph = create_agent(
            ToolModel(
                responses=[
                    mixed,
                    call("project_read", {}, "check"),
                    call(
                        "project_command",
                        {"action": "update_project", "goal": "分开执行后的目标"},
                        "retry",
                    ),
                    AIMessage(content="已恢复"),
                ]
            ),
            middleware=[
                TrajectoryMiddleware(TrajectoryRecorder(tmp_path), stop_on_error=True),
                ProjectStateMiddleware(workflow=work),
                ACPMiddleware(
                    async_subagents=[
                        {
                            "name": "reviewer",
                            "description": "核验",
                            "graph_id": "worker",
                        }
                    ]
                ),
            ],
            store=store,
        )
        result = await graph.ainvoke(
            {"messages": [("user", "继续")]},
            {
                "configurable": {
                    "project_id": "p",
                    "thread_id": "parent",
                    "project_state_single_writer": True,
                }
            },
        )
        outputs = {
            m.tool_call_id: m for m in result["messages"] if isinstance(m, ToolMessage)
        }
        assert all(
            json.loads(outputs[key].content)["error"]["code"]
            == "parallel_project_writes"
            for key in ("write", "follow")
        )
        assert "不应生效" not in outputs["check"].content
        assert not result.get("async_tasks")
        assert (await ProjectStore(store).load("p")).goal == "分开执行后的目标"
        assert not (tmp_path / "first-error.json").exists()

    asyncio.run(scenario())


def test_review_context_distinguishes_missing_receipt_from_current_acceptance():
    async def scenario():
        store, work, _ = await fixture(TaskStatus.REVIEW)
        snapshot = await ProjectStore(store).load("p")
        current = (await work.review_status(snapshot))[0]
        assert current["validation_record_present"] is False
        assert current["validation_accepted_current"] is False
        assert "不代表宿主拒绝" in current["validation_state"]
        await store.aput(
            ("deep_loopminder", "project_validations", "p"), "t", {"subject": "旧版本"}
        )
        current = (await work.review_status(snapshot))[0]
        assert current["validation_record_present"] is True
        assert current["validation_accepted_current"] is False

    asyncio.run(scenario())


def test_active_prerequisite_cannot_be_removed_and_host_restore_preserves_history():
    from dataclasses import replace

    from src.middlewares.project_state.commands import (
        RestoreTaskDependenciesCommand,
        UpdateTaskDependenciesCommand,
    )
    from src.middlewares.project_state.transitions import ProjectStateError

    async def scenario():
        store, _, base = await fixture(TaskStatus.REVIEW)
        base = replace(base, execution_refs=("langgraph-run://base/run",))
        downstream = replace(
            base,
            task_id="feature",
            status=TaskStatus.BACKLOG,
            dependencies=frozenset({"t"}),
            execution_refs=(),
        )
        snapshot = ProjectSnapshot("p", "不能删边绕门禁", (), (base, downstream))
        with pytest.raises(ProjectStateError) as error:
            apply_project_command(
                snapshot, UpdateTaskDependenciesCommand("p", "feature", frozenset())
            )
        assert error.value.details["code"] == "active_dependency_removal_forbidden"
        # 宿主恢复已被旧漏洞误删的边，只增加约束，保持执行、状态及制品历史。
        running = replace(
            downstream,
            dependencies=frozenset(),
            status=TaskStatus.IN_PROGRESS,
            execution_refs=("langgraph-run://feature/run",),
        )
        broken = replace(snapshot, tasks=(base, running))
        repaired = apply_project_command(
            broken,
            RestoreTaskDependenciesCommand(
                "p", "feature", frozenset({"t"}), "project-artifact://historical-proof"
            ),
        )
        task = repaired.tasks[1]
        assert task.dependencies == frozenset({"t"})
        assert (
            task.status == running.status
            and task.execution_refs == running.execution_refs
        )
        assert set(running.artifact_refs) <= set(task.artifact_refs)

    asyncio.run(scenario())


def test_worker_receives_referenced_materials_without_pm_tool_access():
    """有效正文由宿主交接，错误引用显式报告，不让Worker猜宿主存储路径。"""

    async def scenario():
        store, work, _ = await fixture()
        note = await work.artifacts.create("p", "t", "note", "盘点证据正文")
        brief = {"content": note["reference"] + "\nproject-artifact://" + "0" * 64}
        text = await ProjectStateMiddleware(workflow=work)._worker_materials("p", brief)
        assert "盘点证据正文" in text
        assert "无法解析" in text and "不搜索宿主" in text

    asyncio.run(scenario())


@pytest.mark.parametrize("same_task", [False, True])
def test_parallel_dispatch_commits_both_tasks_without_losing_snapshot(same_task):
    """两个独立负责人同批派出，来源关联和业务状态均不能被另一写入覆盖。"""

    async def scenario():
        from dataclasses import replace

        store, work, task = await fixture(TaskStatus.BACKLOG)
        second = await work.artifacts.create("p", "u", "task_brief", "第二项任务")
        await ProjectStore(store).save(
            ProjectSnapshot(
                "p",
                "验证",
                (),
                (
                    task,
                    replace(task, task_id="u", artifact_refs=(second["reference"],)),
                ),
            )
        )
        client = client_fixture()
        client.threads.create.side_effect = [
            {"thread_id": "child-a"},
            {"thread_id": "child-b"},
        ]
        response = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "spawn_agent",
                    "args": {
                        "task_name": key,
                        "task_id": "t" if same_task else key,
                        "message": "完成目标",
                    },
                    "id": key,
                }
                for key in ("t", "u")
            ],
        )
        graph = create_agent(
            ToolModel(responses=[response, AIMessage(content="已安排")]),
            middleware=[
                ProjectStateMiddleware(workflow=work),
                ACPMiddleware(
                    async_subagents=[
                        {
                            "name": "reviewer",
                            "description": "核验",
                            "graph_id": "worker",
                            "url": "http://localhost:1",
                        },
                    ]
                ),
            ],
            store=store,
            checkpointer=InMemorySaver(),
        )
        with (
            patch(
                "deepagents.middleware.async_subagents.get_client", return_value=client
            ),
            patch(
                "src.middlewares.acp.middleware.get_client",
                return_value=client,
            ),
        ):
            result = await graph.ainvoke(
                {"messages": [("user", "安排两项任务")]},
                {
                    "configurable": {
                        "project_id": "p",
                        "thread_id": "parent",
                        "project_state_single_writer": True,
                    },
                },
            )
        outputs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        if same_task:
            assert all(m.status == "error" for m in outputs)
            client.runs.create.assert_not_awaited()
            assert all(
                t.status == TaskStatus.BACKLOG
                for t in (await ProjectStore(store).load("p")).tasks
            )
            return
        assert len(outputs) == 2 and all(m.status != "error" for m in outputs)
        snapshot = await ProjectStore(store).load("p")
        assert all(
            t.status == TaskStatus.IN_PROGRESS and len(t.execution_refs) == 1
            for t in snapshot.tasks
        )
        assert len(result["async_tasks"]) == 2
        assert (
            len(await store.asearch(("deep_loopminder", "project_execution_refs", "p")))
            == 2
        )

    asyncio.run(scenario())


def test_explicit_approval_revocation_keeps_validation_but_blocks_closure():
    async def scenario():
        from dataclasses import replace

        store, work, _, ref = await delivered_fixture()
        await work.record_validation(
            "p",
            "t",
            validator_id="host",
            criteria={"真实交付": True},
            evidence_refs=(ref,),
        )
        snapshot = await ProjectStore(store).load("p")
        snapshot = replace(
            snapshot, tasks=(replace(snapshot.tasks[0], status=TaskStatus.PASSED),)
        )
        await ProjectStore(store).save(snapshot)
        await work.record_approval(
            "p", "t", actor_id="human", decision_ref="fixture://yes"
        )
        assert (await work.facts(snapshot, "t", TaskStatus.APPROVED)).human_approved
        await work.record_approval(
            "p", "t", actor_id="human", decision_ref="fixture://revoke", approved=False
        )
        facts = await work.facts(snapshot, "t", TaskStatus.APPROVED)
        assert facts.validation_accepted and not facts.human_approved

    asyncio.run(scenario())


def test_unknown_task_acceptance_returns_recoverable_error():
    async def scenario():
        store, work, _ = await fixture()
        graph = create_agent(
            ToolModel(
                responses=[
                    call(
                        "project_command",
                        {
                            "action": "transition_task",
                            "task_id": "missing",
                            "target_status": "passed",
                        },
                        "missing",
                    ),
                    call("project_read", {}, "read"),
                    AIMessage(content="已读回"),
                ]
            ),
            middleware=[ProjectStateMiddleware(workflow=work)],
            store=store,
        )
        result = await graph.ainvoke(
            {"messages": [("user", "检查未知任务")]},
            {"configurable": {"project_id": "p", "project_state_single_writer": True}},
        )
        messages = {
            m.tool_call_id: m for m in result["messages"] if isinstance(m, ToolMessage)
        }
        assert messages["missing"].status == "error"
        assert messages["read"].status == "success"

    asyncio.run(scenario())


def test_revalidation_does_not_resurrect_historical_human_approval():
    async def scenario():
        from dataclasses import replace

        store, work, _, ref = await delivered_fixture()

        async def validate(accepted):
            return await work.record_validation(
                "p",
                "t",
                validator_id="host",
                criteria={"真实交付": accepted},
                evidence_refs=(ref,),
            )

        await validate(True)
        snapshot = await ProjectStore(store).load("p")
        await ProjectStore(store).save(
            replace(
                snapshot, tasks=(replace(snapshot.tasks[0], status=TaskStatus.PASSED),)
            )
        )
        await work.record_approval(
            "p", "t", actor_id="human", decision_ref="fixture://yes"
        )
        # 验收重发不是新结论，保持批准；新结论必须撤销旧批准。
        await ProjectStore(store).save(snapshot)
        await validate(True)
        assert (await work.facts(snapshot, "t", TaskStatus.APPROVED)).human_approved
        await validate(False)
        await validate(True)
        facts = await work.facts(snapshot, "t", TaskStatus.APPROVED)
        assert facts.validation_accepted and not facts.human_approved

    asyncio.run(scenario())


def test_review_alias_binds_same_task_and_collects_independent_result():
    """业务编号不改大小写，审查者以独立别名绑定原任务并领取交付。"""

    async def scenario():
        from dataclasses import replace

        store, work, task = await fixture(TaskStatus.REVIEW)
        await ProjectStore(store).save(
            ProjectSnapshot("p", "review", (), (replace(task, owner_role="developer"),))
        )
        client = client_fixture()
        client.runs.list.return_value = [{"run_id": "run-1", "status": "success"}]
        model = ToolModel(
            responses=[
                call(
                    "spawn_agent",
                    {
                        "task_name": "T0_review",
                        "task_id": "t",
                        "agent_type": "reviewer",
                        "message": "独立检查",
                    },
                    "spawn-review",
                ),
                call("wait_agent", {"timeout_ms": 0}, "collect-review"),
                call("project_read", {"task_id": "t"}, "inspect"),
                AIMessage(content="等待宿主验收"),
            ]
        )
        graph = create_agent(
            model,
            middleware=[
                ProjectStateMiddleware(workflow=work),
                ACPMiddleware(
                    async_subagents=[
                        {
                            "name": "reviewer",
                            "description": "review",
                            "graph_id": "worker",
                            "url": "http://localhost:1",
                        }
                    ]
                ),
            ],
            store=store,
        )
        with (
            patch(
                "deepagents.middleware.async_subagents.get_client", return_value=client
            ),
            patch("src.middlewares.acp.middleware.get_client", return_value=client),
        ):
            result = await graph.ainvoke(
                {"messages": [("user", "审查")]},
                {
                    "configurable": {
                        "project_id": "p",
                        "thread_id": "parent",
                        "project_state_single_writer": True,
                    }
                },
            )
        outputs = {
            m.tool_call_id: m for m in result["messages"] if isinstance(m, ToolMessage)
        }
        assert all(m.status == "success" for m in outputs.values())
        envelope = json.loads(
            client.runs.create.await_args.kwargs["input"]["messages"][0]["content"]
        )
        assert envelope["task_id"] == "t" and envelope["task_name"] == "T0_review"
        assert envelope["agent_type"] == "reviewer"
        current = (await ProjectStore(store).load("p")).tasks
        assert len(current) == 1 and current[0].owner_role == "developer"
        assert len(current[0].artifact_refs) == 2
        assert (
            "passed"
            in json.loads(outputs["inspect"].content)["controls"]["gated_transitions"]
        )
        # Worker成功不能凭自身声明创建验收记录。
        assert not (
            await work.facts(
                await ProjectStore(store).load("p"), "t", TaskStatus.PASSED
            )
        ).validation_accepted

    asyncio.run(scenario())


def test_dependency_diagnostics_repair_and_history_protection():
    async def scenario():
        from dataclasses import replace

        from src.middlewares.project_state.commands import UpdateTaskDependenciesCommand
        from src.middlewares.project_state.transitions import task_controls

        store, work, subject = await fixture(TaskStatus.REVIEW)
        review = replace(
            subject,
            task_id="review",
            status=TaskStatus.BACKLOG,
            dependencies=frozenset({"t"}),
            artifact_refs=(),
        )
        snapshot = ProjectSnapshot("p", "依赖恢复", (), (subject, review))
        await ProjectStore(store).save(snapshot)
        controls = task_controls(snapshot, "review")
        assert controls["blocking_dependencies"] == [
            {"task_id": "t", "status": "review"}
        ]
        assert controls["allowed_transitions"] == [] and controls["dependency_editable"]
        updated = apply_project_command(
            snapshot, UpdateTaskDependenciesCommand("p", "review", frozenset())
        )
        assert task_controls(updated, "review")["allowed_transitions"] == ["ready"]
        # 修改后仍执行完整DAG检查，不允许环或引用不存在的任务。
        for dependencies in (frozenset({"review"}), frozenset({"missing"})):
            with pytest.raises(ValueError):
                apply_project_command(
                    snapshot, UpdateTaskDependenciesCommand("p", "review", dependencies)
                )
        with pytest.raises(ValueError, match="尚未执行"):
            apply_project_command(
                snapshot, UpdateTaskDependenciesCommand("p", "t", frozenset())
            )

    asyncio.run(scenario())


def test_post_dispatch_binding_failure_aborts_instead_of_inviting_duplicate_spawn():
    async def scenario():
        store, work, _ = await fixture()
        client = client_fixture()
        model = ToolModel(
            responses=[
                call("spawn_agent", {"task_name": "t", "message": "检查"}),
                AIMessage(content="end"),
            ]
        )
        graph = create_agent(
            model,
            middleware=[
                ProjectStateMiddleware(workflow=work),
                ACPMiddleware(
                    async_subagents=[
                        {
                            "name": "reviewer",
                            "description": "review",
                            "graph_id": "worker",
                            "url": "http://localhost:1",
                        }
                    ]
                ),
            ],
            store=store,
        )
        with (
            patch(
                "deepagents.middleware.async_subagents.get_client", return_value=client
            ),
            patch("src.middlewares.acp.middleware.get_client", return_value=client),
            patch.object(
                work, "record_start", side_effect=ValueError("binding failed")
            ),
        ):
            with pytest.raises(RuntimeError, match="禁止重复派工"):
                await graph.ainvoke(
                    {"messages": [("user", "检查")]},
                    {
                        "configurable": {
                            "project_id": "p",
                            "thread_id": "parent",
                            "project_state_single_writer": True,
                        }
                    },
                )
        assert client.runs.create.await_count == 1

    asyncio.run(scenario())


def test_structured_rejection_explains_gate_and_retry_is_idempotent():
    async def scenario():
        store, work, _ = await fixture(TaskStatus.REVIEW)
        model = ToolModel(
            responses=[
                call(
                    "project_command",
                    {
                        "action": "transition_task",
                        "task_id": "t",
                        "target_status": "passed",
                    },
                    "gate",
                ),
                call(
                    "project_command",
                    {
                        "action": "transition_task",
                        "task_id": "t",
                        "target_status": "review",
                    },
                    "repeat",
                ),
                call("project_read", {"task_id": "missing"}, "unknown"),
                AIMessage(content="等待可信验收"),
            ]
        )
        result = await create_agent(
            model, middleware=[ProjectStateMiddleware(workflow=work)], store=store
        ).ainvoke(
            {"messages": [("user", "门禁诊断")]},
            {"configurable": {"project_id": "p", "project_state_single_writer": True}},
        )
        outputs = {
            m.tool_call_id: m for m in result["messages"] if isinstance(m, ToolMessage)
        }
        gate = json.loads(outputs["gate"].content)["error"]["controls"]
        assert (
            json.loads(outputs["gate"].content)["error"]["code"]
            == "validation_required"
        )
        assert (
            gate["current_status"] == "review" and "passed" in gate["gated_transitions"]
        )
        assert (
            outputs["repeat"].status == "success"
            and outputs["unknown"].status == "error"
        )
        assert (await ProjectStore(store).load("p")).tasks[
            0
        ].status == TaskStatus.REVIEW

    asyncio.run(scenario())


def test_review_testing_can_switch_without_bypassing_trusted_validation():
    from src.middlewares.project_state.commands import TransitionTaskCommand
    from src.middlewares.project_state.transitions import ProjectStateError

    task = Task(
        "t", "交付", TaskStatus.REVIEW, "developer", frozenset(), frozenset(), ("通过",)
    )
    snapshot = ProjectSnapshot("p", "交付", (), (task,))
    for target in (TaskStatus.TESTING, TaskStatus.REVIEW):
        snapshot = apply_project_command(
            snapshot, TransitionTaskCommand("p", "t", target)
        )
        with pytest.raises(ProjectStateError) as caught:
            apply_project_command(
                snapshot, TransitionTaskCommand("p", "t", TaskStatus.PASSED)
            )
        assert caught.value.details["code"] == "validation_required"


@pytest.mark.parametrize(
    "status", [TaskStatus.PASSED, TaskStatus.APPROVED, TaskStatus.DONE]
)
def test_terminal_status_replay_rechecks_trusted_facts(status):
    from src.middlewares.project_state.commands import TransitionTaskCommand
    from src.middlewares.project_state.transitions import (
        TransitionDecision,
        transition_decision,
    )

    task = Task("t", "验收", status, "developer", frozenset(), frozenset(), ("通过",))
    snapshot = ProjectSnapshot("p", "验收", (), (task,))
    stale = TransitionTaskCommand("p", "t", status)
    valid = TransitionTaskCommand(
        "p", "t", status, validation_accepted=True, human_approved=True
    )
    assert transition_decision(snapshot, stale) is TransitionDecision.REJECTED
    assert transition_decision(snapshot, valid) is TransitionDecision.ALLOWED


@pytest.mark.parametrize("duplicate", [False, True])
def test_parallel_artifacts_do_not_mutate_snapshot_and_keep_content_identity(
    tmp_path, duplicate
):
    """覆盖导致实跑中断的8调用，并同时验证重复内容的幂等引用。"""

    async def scenario():
        from src.middlewares.trajectory import TrajectoryMiddleware, TrajectoryRecorder

        store, work, _ = await fixture()
        before = await ProjectStore(store).load("p")
        calls = [
            {
                "name": "project_create_artifact",
                "args": {
                    "task_id": "T1" if duplicate else f"T{index}",
                    "content": "# 独立任务说明"
                    if duplicate
                    else f"# 任务{index}\n验收条件{index}",
                    "kind": "task_brief",
                },
                "id": f"brief-{index}",
            }
            for index in range(1, 9)
        ]
        model = ToolModel(
            responses=[
                AIMessage(content="", tool_calls=calls),
                AIMessage(content="created"),
            ]
        )
        recorder = TrajectoryRecorder(tmp_path)
        graph = create_agent(
            model,
            middleware=[
                TrajectoryMiddleware(recorder, stop_on_error=True),
                ProjectStateMiddleware(workflow=work),
            ],
            store=store,
        )
        result = await graph.ainvoke(
            {"messages": [("user", "创建8份说明")]},
            {
                "configurable": {
                    "project_id": "p",
                    "thread_id": "batch",
                    "project_state_single_writer": True,
                }
            },
        )
        outputs = [
            message
            for message in result["messages"]
            if isinstance(message, ToolMessage)
        ]
        assert len(outputs) == 8 and all(
            message.status == "success" for message in outputs
        )
        refs = [json.loads(message.content)["reference"] for message in outputs]
        assert len(set(refs)) == (1 if duplicate else 8)
        for request, reference in zip(calls, refs, strict=True):
            artifact = await work.artifacts.get(
                "p", reference, task_id=request["args"]["task_id"]
            )
            assert artifact["content"] == request["args"]["content"]
        records = await store.asearch(
            ("deep_loopminder", "project_artifacts", "p"), limit=100
        )
        assert len(records) == (2 if duplicate else 9)  # fixture已有一份说明。
        assert await ProjectStore(store).load("p") == before
        assert not (tmp_path / "first-error.json").exists()
        assert result["messages"][-1].content == "created"

    asyncio.run(scenario())


@pytest.mark.parametrize("writer,mixed", [(False, False), (True, True)])
def test_parallel_artifact_exemption_preserves_readonly_and_mixed_batch_protection(
    writer, mixed
):
    async def scenario():
        store, work, _ = await fixture()
        before = await ProjectStore(store).load("p")
        calls = [
            {
                "name": "project_create_artifact",
                "args": {"task_id": "new", "content": "不得部分写入"},
                "id": "brief",
            }
        ]
        if mixed:
            calls.append(
                {
                    "name": "project_command",
                    "args": {"action": "update_project", "goal": "不得修改"},
                    "id": "mutate",
                }
            )
        else:
            calls.append(
                {
                    "name": "project_create_artifact",
                    "args": {"task_id": "other", "content": "只读拒绝"},
                    "id": "other",
                }
            )
        graph = create_agent(
            ToolModel(
                responses=[
                    AIMessage(content="", tool_calls=calls),
                    AIMessage(content="blocked"),
                ]
            ),
            middleware=[ProjectStateMiddleware(workflow=work)],
            store=store,
        )
        result = await graph.ainvoke(
            {"messages": [("user", "验证写入边界")]},
            {
                "configurable": {
                    "project_id": "p",
                    "project_state_single_writer": writer,
                }
            },
        )
        outputs = [
            message
            for message in result["messages"]
            if isinstance(message, ToolMessage)
        ]
        assert len(outputs) == 2 and all(
            message.status == "error" for message in outputs
        )
        if mixed:
            assert all(
                json.loads(message.content)["error"]["code"]
                == "parallel_project_writes"
                for message in outputs
            )
        assert await ProjectStore(store).load("p") == before
        assert (
            len(
                await store.asearch(
                    ("deep_loopminder", "project_artifacts", "p"), limit=100
                )
            )
            == 1
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status", [TaskStatus.BACKLOG, TaskStatus.READY, TaskStatus.ASSIGNED]
)
def test_dispatch_advances_business_state_only_after_confirmed_run(status):
    async def scenario():
        store, work, _ = await fixture(status)
        await work.prepare_spawn("p", "t", "reviewer")
        assert (await ProjectStore(store).load("p")).tasks[0].status is status
        await work.record_start("p", "t", "parent", "worker", "confirmed-run")
        task = (await ProjectStore(store).load("p")).tasks[0]
        assert task.status is TaskStatus.IN_PROGRESS
        assert task.execution_refs == ("langgraph-run://worker/confirmed-run",)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status", [TaskStatus.BACKLOG, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW]
)
def test_dispatch_does_not_bypass_unfinished_dependencies(status):
    async def scenario():
        from dataclasses import replace

        store, work, task = await fixture(status)
        dependency = replace(task, task_id="dependency", status=TaskStatus.IN_PROGRESS)
        await ProjectStore(store).save(
            ProjectSnapshot(
                "p",
                "dispatch",
                (),
                (
                    dependency,
                    replace(task, dependencies=frozenset({"dependency"})),
                ),
            )
        )
        with pytest.raises(ValueError, match="前置依赖"):
            await work.prepare_spawn("p", "t", "reviewer")
        assert not (await ProjectStore(store).load("p")).tasks[1].execution_refs

    asyncio.run(scenario())


def test_interrupted_run_recovery_keeps_both_failure_observations():
    """取消收尾与恢复状态不同仍是失败，保留两份不可变证据并拒绝改成成功。"""

    async def scenario():
        store, work, _ = await fixture()
        await work.record_start("p", "t", "pm", "worker", "run")
        base = {"agent_id": "worker", "run_id": "run"}
        interrupted = {
            **base,
            "status": "interrupted",
            "error": {"code": "run_interrupted"},
        }
        failed = {
            **base,
            "status": "error",
            "error": {"code": "run_error", "details": ["Connection closed"]},
        }
        first = await work.record_result("p", "pm", interrupted)
        second = await work.record_result("p", "pm", failed)
        assert first != second
        assert await work.record_result("p", "pm", failed) == second
        binding = await store.aget(
            ("deep_loopminder", "project_execution_refs", "p"), "run"
        )
        assert binding.value["result_ref"] == second
        assert binding.value["previous_result_refs"] == [first]
        task = (await ProjectStore(store).load("p")).tasks[0]
        assert first in task.artifact_refs and second in task.artifact_refs
        assert (
            json.loads((await work.artifacts.get("p", first))["content"])["status"]
            == "interrupted"
        )
        with pytest.raises(ValueError, match="不能返回不同交付内容"):
            await work.record_result(
                "p", "pm", {**base, "status": "success", "result": "passed"}
            )
        assert (
            await store.aget(("deep_loopminder", "project_execution_refs", "p"), "run")
        ).value["result_ref"] == second

    asyncio.run(scenario())
