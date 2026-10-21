"""Exercise the public tools through the official Agent graph."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

pytest.importorskip("acp")

from src.middlewares.acp import ACPMiddleware
from src.middlewares.acp.graph import create_worker_graph
from src.runtime.routing import WorkerCandidate


class ToolModel(FakeMessagesListChatModel):
    seen_system: list[str] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        self.seen_system.append(messages[0].text)
        return super()._generate(messages, *args, **kwargs)


def build_agent(calls):
    middleware = ACPMiddleware(
        async_subagents=[
            {
                "name": "researcher",
                "description": "Inspect the project",
                "graph_id": "acp_worker",
                "url": "http://localhost:1",
            }
        ]
    )
    responses = []
    for index, (name, args) in enumerate(calls):
        responses.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": name, "args": args, "id": f"call-{index}"}],
                ),
                AIMessage(content="OK"),
            ]
        )
    return create_agent(
        ToolModel(responses=responses),
        middleware=[middleware],
        checkpointer=InMemorySaver(),
    )


def client_fixture():
    runs = SimpleNamespace(
        create=AsyncMock(
            side_effect=[
                {"run_id": "run-1", "status": "pending"},
                {"run_id": "run-2", "status": "pending"},
            ]
        ),
        get=AsyncMock(return_value={"status": "success"}),
        join=AsyncMock(return_value={}),
        list=AsyncMock(return_value=[]),
        cancel=AsyncMock(),
        wait=AsyncMock(
            return_value={"messages": [{"content": '{"outcome":"injected"}'}]}
        ),
    )

    # 官方观察钩子要求 Run 包含标识；保留各用例设置状态的方式。
    async def get_run(*args, **kwargs):
        run_id = kwargs.get("run_id") or args[1]
        return {**runs.get.return_value, "run_id": run_id}

    runs.get.side_effect = get_run
    threads = SimpleNamespace(
        create=AsyncMock(return_value={"thread_id": "child"}),
        get=AsyncMock(
            return_value={
                "values": {"messages": [{"type": "ai", "content": "ACP_RESULT"}]}
            }
        ),
        get_history=AsyncMock(
            side_effect=lambda *args, **kwargs: [
                {
                    "values": {
                        "owner_thread_id": "parent",
                        "messages": [
                            {
                                "type": "ai",
                                "content": "ACP_RESULT",
                                "additional_kwargs": {
                                    "acp_run_id": kwargs["metadata"]["run_id"]
                                },
                            }
                        ],
                    },
                    "next": [],
                    "checkpoint": {"checkpoint_id": "final"},
                }
            ]
        ),
    )
    return SimpleNamespace(runs=runs, threads=threads)


def test_subagent_tools_preserve_official_state_and_queue_followups():
    async def scenario():
        client = client_fixture()
        calls = [
            (
                "spawn_agent",
                {
                    "task_name": "inspect",
                    "message": "Inspect",
                    "agent_type": "researcher",
                },
            ),
            ("wait_agent", {"timeout_ms": 100}),
            ("followup_task", {"target": "child", "message": "Continue"}),
            ("send_message", {"target": "child", "message": "Prioritize ACP"}),
            ("list_agents", {}),
            ("wait_agent", {"timeout_ms": 100}),
        ]
        agent = build_agent(calls)
        config = {"configurable": {"thread_id": "parent", "project_id": "project"}}
        with (
            patch(
                "deepagents.middleware.async_subagents.get_client", return_value=client
            ),
            patch("src.middlewares.acp.middleware.get_client", return_value=client),
        ):
            for _ in calls:
                result = await agent.ainvoke(
                    {"messages": [("human", "Perform the next operation")]}, config
                )
        assert result["async_tasks"]["child"]["thread_id"] == "child"
        assert result["async_tasks"]["child"]["run_id"] == "run-2"
        assert result["acp_task_names"] == {"child": "inspect"}
        create_calls = client.runs.create.await_args_list
        first = json.loads(create_calls[0].kwargs["input"]["messages"][0]["content"])
        assert first["owner_thread_id"] == "parent"
        assert first["project_id"] == "project"
        assert create_calls[1].kwargs["multitask_strategy"] == "enqueue"
        assert create_calls[1].kwargs["thread_id"] == "child"
        assert (
            client.runs.wait.await_args.kwargs["input"]["owner_thread_id"] == "parent"
        )
        tool_outputs = [
            json.loads(m.content)
            for m in result["messages"]
            if isinstance(m, ToolMessage)
        ]
        assert tool_outputs[0]["agent_id"] == "child"
        assert not tool_outputs[0]["status_fresh"]
        assert tool_outputs[1]["completed"][0]["result"] == "ACP_RESULT"
        assert tool_outputs[3]["outcome"] == "injected"
        assert tool_outputs[-1]["completed"][0]["result"] == "ACP_RESULT"

    asyncio.run(scenario())


async def drive(calls, client):
    agent = build_agent(calls)
    config = {"configurable": {"thread_id": "parent", "project_id": "project"}}
    outputs = []
    with (
        patch("deepagents.middleware.async_subagents.get_client", return_value=client),
        patch("src.middlewares.acp.middleware.get_client", return_value=client),
    ):
        for _ in calls:
            result = await agent.ainvoke(
                {"messages": [("human", "Next operation")]}, config
            )
            message = next(
                m for m in reversed(result["messages"]) if isinstance(m, ToolMessage)
            )
            outputs.append(json.loads(message.content))
    return result, outputs


def test_alias_default_role_and_validation_do_not_dispatch_invalid_requests():
    async def scenario():
        client = client_fixture()
        calls = [
            ("spawn_agent", {"task_name": "inspect", "message": "Inspect"}),
            ("followup_task", {"target": "inspect", "message": "Continue"}),
            ("spawn_agent", {"task_name": "inspect", "message": "Duplicate"}),
            (
                "spawn_agent",
                {
                    "task_name": "other",
                    "message": "Inspect",
                    "agent_type": "unconfigured",
                },
            ),
            ("send_message", {"target": "inspect", "message": "   "}),
            ("list_agents", {"path_prefix": "ins", "status": "success"}),
        ]
        _, outputs = await drive(calls, client)
        assert outputs[0]["agent_type"] == "researcher"
        assert outputs[1]["agent_id"] == "child"
        assert outputs[2]["error"]["code"] == "task_name_exists"
        assert outputs[3]["error"]["available_agent_types"] == ["researcher"]
        assert outputs[4]["error"]["code"] == "empty_message"
        assert outputs[5]["agents"][0]["task_name"] == "inspect"
        assert client.runs.create.await_count == 2
        client.runs.wait.assert_not_awaited()

    asyncio.run(scenario())


def test_queued_turn_results_are_distinct_and_delivery_survives_parent_turns():
    async def scenario():
        client = client_fixture()
        client.runs.list.side_effect = lambda *args, **kwargs: [
            {"run_id": "run-2", "status": "success", "created_at": "2"},
            {"run_id": "run-1", "status": "success", "created_at": "1"},
        ]

        async def history(*args, **kwargs):
            run_id = kwargs["metadata"]["run_id"]
            return [
                {
                    "values": {
                        "owner_thread_id": "parent",
                        "messages": [
                            {
                                "type": "ai",
                                "content": run_id,
                                "additional_kwargs": {"acp_run_id": run_id},
                            }
                        ],
                    },
                    "next": [],
                    "checkpoint": {"checkpoint_id": run_id},
                }
            ]

        client.threads.get_history.side_effect = history
        calls = [
            ("spawn_agent", {"task_name": "inspect", "message": "Inspect"}),
            ("followup_task", {"target": "inspect", "message": "Continue"}),
            ("wait_agent", {"targets": ["inspect"], "timeout_ms": 0}),
            ("wait_agent", {"targets": ["child"], "timeout_ms": 0}),
            (
                "wait_agent",
                {
                    "targets": ["inspect"],
                    "include_completed": True,
                    "timeout_ms": 0,
                    "run_id": "run-1",
                },
            ),
            (
                "wait_agent",
                {"targets": ["inspect"], "include_completed": True, "timeout_ms": 0},
            ),
        ]
        result, outputs = await drive(calls, client)
        assert [item["result"] for item in outputs[2]["completed"]] == [
            "run-1",
            "run-2",
        ]
        assert outputs[3]["completed"] == []
        assert [item["run_id"] for item in outputs[4]["completed"]] == ["run-1"]
        assert outputs[5]["completed"] == []
        assert "未读结果模式" in outputs[5]["notice"]
        assert result["acp_delivered"] == {"run-1": True, "run-2": True}
        # 官方钩子可读取最新线程作状态通知；逐轮结果仍必须来自对应检查点。
        assert all(
            "run_id" in call.kwargs["metadata"]
            for call in client.threads.get_history.await_args_list
        )

    asyncio.run(scenario())


def test_result_not_available_remains_unread_and_interrupt_is_idempotent():
    async def scenario():
        client = client_fixture()
        client.threads.get_history.side_effect = None
        client.threads.get_history.return_value = []
        calls = [
            ("spawn_agent", {"task_name": "inspect", "message": "Inspect"}),
            ("wait_agent", {"timeout_ms": 0}),
            ("interrupt_agent", {"target": "inspect"}),
            ("interrupt_agent", {"target": "child"}),
        ]
        result, outputs = await drive(calls, client)
        assert outputs[1]["errors"][0]["code"] == "result_unavailable"
        assert not result["acp_delivered"]
        assert outputs[2]["interruption_complete"]
        assert not outputs[2]["cancellation_requested"]
        assert outputs[3]["interruption_complete"]
        client.runs.cancel.assert_not_awaited()

    asyncio.run(scenario())


def test_partial_remote_failure_keeps_healthy_results_and_marks_stale_status():
    async def scenario():
        client = client_fixture()
        client.threads.create.side_effect = [
            {"thread_id": "child"},
            {"thread_id": "other"},
        ]

        async def runs(thread_id, **kwargs):
            if thread_id == "other":
                raise httpx.ConnectError("offline")
            return [{"run_id": "run-1", "status": "success"}]

        client.runs.list.side_effect = runs
        get_run = client.runs.get.side_effect

        async def observe(*args, **kwargs):
            if (kwargs.get("thread_id") or args[0]) == "other":
                raise httpx.ConnectError("offline")
            return await get_run(*args, **kwargs)

        client.runs.get.side_effect = observe
        calls = [
            ("spawn_agent", {"task_name": "inspect", "message": "Inspect"}),
            ("spawn_agent", {"task_name": "second", "message": "Inspect"}),
            ("wait_agent", {"targets": ["inspect", "second"], "timeout_ms": 0}),
        ]
        result, outputs = await drive(calls, client)
        assert outputs[2]["completed"][0]["agent_id"] == "child"
        assert outputs[2]["errors"][0]["agent_id"] == "other"
        assert outputs[2]["errors"][0]["code"] == "server_unavailable"
        assert not outputs[2]["agents"][1]["status_fresh"]
        assert result["async_tasks"]["other"]["status"] == "running"

    asyncio.run(scenario())


def test_wait_timeout_does_not_cancel_work_and_interrupt_uses_live_status():
    async def scenario():
        client = client_fixture()
        release = asyncio.Event()

        async def join(*args, **kwargs):
            await release.wait()
            return {}

        async def cancel(*args, **kwargs):
            release.set()
            client.runs.get.return_value = {"status": "interrupted"}

        client.runs.join.side_effect = join
        client.runs.cancel.side_effect = cancel
        client.runs.get.return_value = {"status": "running"}
        calls = [
            (
                "spawn_agent",
                {
                    "task_name": "inspect",
                    "message": "Inspect",
                    "agent_type": "researcher",
                },
            ),
            ("wait_agent", {"timeout_ms": 10}),
            ("interrupt_agent", {"target": "child"}),
        ]
        agent = build_agent(calls)
        config = {"configurable": {"thread_id": "parent"}}
        with (
            patch(
                "deepagents.middleware.async_subagents.get_client", return_value=client
            ),
            patch("src.middlewares.acp.middleware.get_client", return_value=client),
        ):
            await agent.ainvoke({"messages": [("human", "Start")]}, config)
            result = await agent.ainvoke(
                {"messages": [("human", "Wait briefly")]}, config
            )
            assert result["async_tasks"]["child"]["status"] == "running"
            client.runs.cancel.assert_not_awaited()
            result = await agent.ainvoke({"messages": [("human", "Interrupt")]}, config)
        assert result["async_tasks"]["child"]["status"] == "interrupted"
        client.runs.cancel.assert_awaited_once_with("child", "run-1", wait=False)

    asyncio.run(scenario())


def test_foreign_target_is_rejected_before_any_control_dispatch():
    async def scenario():
        client = client_fixture()
        agent = build_agent(
            [("send_message", {"target": "foreign", "message": "Change your task"})]
        )
        with patch("src.middlewares.acp.middleware.get_client", return_value=client):
            result = await agent.ainvoke(
                {"messages": [("human", "Send")]},
                {"configurable": {"thread_id": "other-parent"}},
            )
        output = next(m for m in result["messages"] if isinstance(m, ToolMessage))
        assert json.loads(output.content)["error"]["code"] == "unknown_target"
        client.runs.wait.assert_not_awaited()

    asyncio.run(scenario())


def test_missing_checkpoint_reference_cannot_create_a_replacement_session():
    async def scenario():
        graph = create_worker_graph({}, ())
        with pytest.raises(RuntimeError, match="reference is missing"):
            await graph.ainvoke(
                {
                    "messages": [
                        HumanMessage(content=json.dumps({"operation": "followup"}))
                    ]
                },
                {"configurable": {"thread_id": "lost-checkpoint"}},
            )

    from langchain_core.messages import HumanMessage

    asyncio.run(scenario())


def test_queue_is_visible_and_cancelled_before_running_turn():
    async def scenario():
        client = client_fixture()
        statuses = {"run-1": "running", "run-2": "pending"}
        client.runs.list.side_effect = lambda *args, **kwargs: [
            {"run_id": key, "status": value} for key, value in statuses.items()
        ]

        async def cancel(thread_id, run_id, **kwargs):
            statuses[run_id] = "interrupted"

        client.runs.cancel.side_effect = cancel
        calls = [
            ("spawn_agent", {"task_name": "inspect", "message": "Inspect"}),
            ("followup_task", {"target": "inspect", "message": "Continue"}),
            ("list_agents", {"status": "running"}),
            ("interrupt_agent", {"target": "inspect"}),
            ("interrupt_agent", {"target": "inspect"}),
        ]
        _, outputs = await drive(calls, client)
        entry = outputs[2]["agents"][0]
        assert entry["status"] == "running"
        assert entry["latest_run_status"] == "pending"
        assert entry["running_run_ids"] == ["run-1"]
        assert entry["queued_run_ids"] == ["run-2"]
        assert [call.args[1] for call in client.runs.cancel.await_args_list] == [
            "run-2",
            "run-1",
        ]
        assert outputs[3]["interruption_complete"]
        assert not outputs[4]["cancellation_requested"]

    asyncio.run(scenario())


def test_mutation_timeout_is_actionable_and_does_not_advance_tracked_run():
    async def scenario():
        client = client_fixture()
        client.runs.create.side_effect = [
            {"run_id": "run-1", "status": "pending"},
            httpx.ReadTimeout("no response"),
        ]
        result, outputs = await drive(
            [
                ("spawn_agent", {"task_name": "inspect", "message": "Inspect"}),
                ("followup_task", {"target": "inspect", "message": "Continue"}),
            ],
            client,
        )
        error = outputs[1]["error"]
        assert error["code"] == "request_timeout"
        assert error["outcome_unknown"]
        assert not error["retryable"]
        assert result["async_tasks"]["child"]["run_id"] == "run-1"

    asyncio.run(scenario())


def test_specific_run_replay_uses_its_checkpoint_and_rejects_foreign_run():
    async def scenario():
        client = client_fixture()
        result, outputs = await drive(
            [
                ("spawn_agent", {"task_name": "inspect", "message": "Inspect"}),
                ("wait_agent", {"targets": ["inspect"], "timeout_ms": 0}),
                (
                    "wait_agent",
                    {"targets": ["inspect"], "run_id": "run-1", "timeout_ms": 0},
                ),
                (
                    "wait_agent",
                    {"targets": ["inspect"], "run_id": "foreign", "timeout_ms": 0},
                ),
            ],
            client,
        )
        assert outputs[2]["completed"][0]["run_id"] == "run-1"
        assert outputs[3]["error"]["code"] == "unknown_run"
        assert result["acp_delivered"] == {"run-1": True}
        assert client.threads.get_history.await_count == 2

    asyncio.run(scenario())


def test_parallel_duplicate_names_are_rejected_without_creating_workers():
    async def scenario():
        client = client_fixture()
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "spawn_agent",
                            "id": f"call-{i}",
                            "args": {"task_name": "inspect", "message": "Inspect"},
                        }
                        for i in range(2)
                    ],
                ),
                AIMessage(content="OK"),
            ]
        )
        middleware = ACPMiddleware(
            async_subagents=[
                {
                    "name": "researcher",
                    "description": "Inspect",
                    "graph_id": "acp_worker",
                }
            ]
        )
        for tool in middleware.tools:
            assert (
                "runtime" not in tool.tool_call_schema.model_json_schema()["properties"]
            )
        graph = create_agent(model, middleware=[middleware])
        with patch(
            "deepagents.middleware.async_subagents.get_client", return_value=client
        ):
            result = await graph.ainvoke(
                {"messages": [("human", "Start")]},
                {"configurable": {"thread_id": "parent"}},
            )
        outputs = [
            json.loads(m.content)
            for m in result["messages"]
            if isinstance(m, ToolMessage)
        ]
        assert all(
            item["error"]["code"] == "duplicate_parallel_name" for item in outputs
        )
        client.threads.create.assert_not_awaited()

    asyncio.run(scenario())


def test_server_startup_failure_closes_resources_and_clears_graph_references():
    from contextlib import asynccontextmanager

    from src.middlewares.acp import server

    async def scenario():
        closed = []

        @asynccontextmanager
        async def worker(*args, **kwargs):
            try:
                yield SimpleNamespace(initialized=None)
            finally:
                closed.append(True)

        settings = {
            "provider_id": "duplicate",
            "command": ["adapter"],
            "cwd": ".",
            "roles": ["researcher"],
            "grants": [],
        }
        with (
            patch.dict(
                "os.environ", {"ACP_WORKERS_JSON": json.dumps([settings, settings])}
            ),
            patch.object(server, "ACPWorker", worker),
            patch.object(server, "worker_candidate", return_value=object()),
        ):
            with pytest.raises(ValueError, match="duplicate ACP provider"):
                async with server.lifespan(server.app):
                    pytest.fail(
                        "Invalid configuration must fail before serving requests"
                    )
        assert closed == [True]
        assert server._workers == {}
        assert server._candidates == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "required, dispatched", [("files.write", True), ("full_stack_development", False)]
)
def test_capability_catalog_is_visible_and_dispatch_is_checked_before_run(
    required, dispatched
):
    async def scenario():
        client = client_fixture()
        middleware = ACPMiddleware(
            async_subagents=[
                {
                    "name": "developer",
                    "description": "Implement",
                    "graph_id": "acp_worker",
                }
            ],
            candidate_source=lambda: [
                WorkerCandidate(
                    "claude",
                    "external_acp",
                    frozenset({"developer"}),
                    frozenset({"files.write"}),
                    True,
                )
            ],
        )
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "spawn_agent",
                            "args": {
                                "task_name": "build",
                                "message": "Build",
                                "agent_type": "developer",
                            },
                            "id": "spawn",
                        }
                    ],
                ),
                AIMessage(content="stop"),
            ]
        )
        graph = create_agent(model, middleware=[middleware])
        with (
            patch(
                "deepagents.middleware.async_subagents.get_client", return_value=client
            ),
            patch("src.middlewares.acp.middleware.get_client", return_value=client),
        ):
            result = await graph.ainvoke(
                {"messages": [("human", "Build")]},
                {
                    "configurable": {
                        "thread_id": "parent",
                        "required_capabilities": [required],
                    },
                },
            )
        assert "files.write" in model.seen_system[0]
        output = json.loads(
            next(m.content for m in result["messages"] if isinstance(m, ToolMessage))
        )
        if dispatched:
            assert output["agent_id"] == "child"
            assert client.runs.create.await_count == 1
        else:
            assert output["error"]["code"] == "worker_requirements_unmatched"
            assert output["error"]["fatal"] is True
            assert required in str(output["error"]["rejected_reasons"])
            client.runs.create.assert_not_awaited()
            client.threads.create.assert_not_awaited()

    asyncio.run(scenario())


def test_failed_session_without_native_reference_cannot_enqueue_followup():
    async def scenario():
        client = client_fixture()
        client.runs.get.return_value = {"status": "error"}
        client.threads.get_state = AsyncMock(return_value={"values": {}})
        _, outputs = await drive(
            [
                ("spawn_agent", {"task_name": "inspect", "message": "Inspect"}),
                ("followup_task", {"target": "inspect", "message": "Retry"}),
            ],
            client,
        )
        assert outputs[-1]["error"]["code"] == "session_not_created"
        assert outputs[-1]["error"]["next_action"] == "stop_and_report"
        assert client.runs.create.await_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("has_session", [False, True])
def test_failed_run_recovery_advice_depends_on_native_session(has_session):
    async def scenario():
        client = client_fixture()
        client.runs.get.return_value = {"run_id": "run-1", "status": "error"}
        original_history = client.threads.get_history.side_effect

        def history(*args, **kwargs):
            rows = original_history(*args, **kwargs)
            if has_session:
                rows[0]["values"]["worker_ref"] = {"session_id": "native"}
            return rows

        client.threads.get_history.side_effect = history
        _, outputs = await drive(
            [
                ("spawn_agent", {"task_name": "inspect", "message": "Inspect"}),
                ("wait_agent", {"targets": ["inspect"], "timeout_ms": 0}),
            ],
            client,
        )
        error = outputs[-1]["completed"][0]["error"]
        assert error["retryable"] is has_session
        assert error["fatal"] is not has_session
        assert error["next_action"] == (
            "followup_task" if has_session else "stop_and_report"
        )

    asyncio.run(scenario())
