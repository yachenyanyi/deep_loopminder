"""Key continuity and official file/structured-output integration checks."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import Field

from src.middlewares.memory.pm_agent_memory import (
    PMAgentMemoryMiddleware,
    PMMemory,
    PMMemorySummarizationMiddleware,
)
from src.middlewares.memory.pm_agent_memory.files import (
    latest_handoff,
    memory_candidates,
    memory_files,
    read_document,
)
from src.middlewares.project_state import (
    ProjectSnapshot,
    ProjectStateMiddleware,
    ProjectStore,
)


class ToolModel(FakeMessagesListChatModel):
    seen: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        self.seen.append(messages)
        return super()._generate(messages, *args, **kwargs)


def test_failed_run_blocks_compaction_and_successful_handoff_reorients_pm():
    async def scenario():
        store = InMemoryStore()
        await ProjectStore(store).save(
            ProjectSnapshot("project", "Ship the latest goal", (), ())
        )
        runs = SimpleNamespace(
            create=AsyncMock(return_value={"run_id": "run-1"}),
            get=AsyncMock(return_value={"run_id": "run-1", "status": "error"}),
            join=AsyncMock(),
        )
        memory = PMMemory(store, client=SimpleNamespace(runs=runs))
        model = ToolModel(responses=[AIMessage(content="Continue")])
        summary = FakeMessagesListChatModel(
            responses=[AIMessage(content="Earlier work summarized")]
        )
        graph = create_agent(
            model,
            middleware=[
                ProjectStateMiddleware(),
                PMAgentMemoryMiddleware(memory),
                PMMemorySummarizationMiddleware(
                    memory, summary, token_threshold=100, keep_messages=1
                ),
            ],
            store=store,
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"project_id": "project", "thread_id": "pm"}}
        messages = [
            HumanMessage(content="original evidence " * 200),
            AIMessage(content="Verified"),
            HumanMessage(content="Continue"),
        ]
        with pytest.raises(RuntimeError, match="Memory run"):
            await graph.ainvoke({"messages": messages}, config)
        assert not model.seen
        failed_source = await memory.source(
            "project", runs.create.call_args.kwargs["input"]["source_id"]
        )
        assert failed_source["snapshot"][0]["data"]["content"] == messages[0].content

        async def finish_run(thread_id, run_id):
            source = await memory.source(
                "project", runs.create.call_args.kwargs["input"]["source_id"]
            )
            await memory_files(store, "project").awrite(
                source["destination"].removeprefix("/memories"),
                "# Handoff\nVerified evidence; next action is deploy.",
            )
            await store.aput(
                memory.namespace("project"),
                source["id"],
                {**source, "handoff_ready": True},
                index=False,
            )

        runs.get.return_value = {"run_id": "run-1", "status": "success"}
        runs.join.side_effect = finish_run
        result = await graph.ainvoke({"messages": []}, config)
        path = await latest_handoff(store, "project", "pm")
        assert path and "next action is deploy" in await read_document(
            store, "project", path
        )
        assert "Ship the latest goal" in model.seen[0][0].text
        assert "next action is deploy" in model.seen[0][0].text
        assert any(
            message.additional_kwargs.get("lc_source") == "summarization"
            for message in result["messages"]
        )

    asyncio.run(scenario())


def test_writer_uses_official_tools_structured_output_and_newer_decisions():
    async def scenario():
        store = InMemoryStore()
        client = SimpleNamespace(runs=SimpleNamespace())
        memory = PMMemory(store, client=client)
        await ProjectStore(store).save(ProjectSnapshot("project", "Old route", (), ()))
        source = await memory.capture(
            "project",
            "pm",
            [HumanMessage(content="Old source")],
            purpose="Review the decision",
        )
        await ProjectStore(store).save(
            ProjectSnapshot("project", "New confirmed route", (), ())
        )
        await memory.observe(
            "project", "pm", [HumanMessage(content="The old route was rejected")]
        )
        memory.model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "write",
                            "name": "write_file",
                            "args": {
                                "file_path": source["destination"],
                                "content": "# Daily\nRevised route from confirmed evidence.",
                            },
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "outcome",
                            "name": "MemoryOutcome",
                            "args": {"outcome": "candidate"},
                        }
                    ],
                ),
            ]
        )
        result = await memory.write(source)
        assert result["outcome"] == "candidate"
        payload = json.loads(memory.model.seen[0][1].text)
        assert payload["latest_project_state"]["goal"] == "New confirmed route"
        assert payload["snapshot"][0]["data"]["content"] == "Old source"
        assert (
            payload["latest_observations"]["observation"]
            == "The old route was rejected"
        )
        candidates = await memory_candidates(store, "project", "Revised route")
        assert candidates[0]["path"] == source["destination"]
        assert candidates[0]["source_refs"] == [source["bookmark"]]
        assert "status" not in await memory.source("project", source["id"])

    asyncio.run(scenario())


def test_registered_memory_graph_executes_snapshot_job(monkeypatch):
    """迁移后的官方图入口能读取固定快照并执行记忆写入。"""
    import importlib
    import sys
    from pathlib import Path

    async def scenario():
        config = json.loads(Path("langgraph.json").read_text(encoding="utf-8"))
        module_name, factory_name = config["graphs"]["pm_memory_agent"].split(":")
        module = importlib.import_module(module_name)
        model = ToolModel(responses=[AIMessage(content="unused")])
        monkeypatch.setitem(
            sys.modules,
            "src.models.llm",
            SimpleNamespace(
                get_default_model=lambda: model,
                get_easycli_model=lambda: model,
                get_command_goat_model=lambda: None,
            ),
        )
        store = InMemoryStore()
        source = {"id": "snapshot-1", "project_id": "p"}
        memory = SimpleNamespace(
            store=store,
            source=AsyncMock(return_value=source),
            write=AsyncMock(return_value={"outcome": "written"}),
        )
        monkeypatch.setattr(module, "PMMemory", lambda store, model: memory)
        monkeypatch.setattr(
            module,
            "get_pm_memory_store",
            AsyncMock(return_value=SimpleNamespace(store=store)),
        )
        graph = await getattr(module, factory_name)()
        result = await graph.ainvoke({"project_id": "p", "source_id": "snapshot-1"})
        memory.source.assert_awaited_once_with("p", "snapshot-1")
        memory.write.assert_awaited_once_with(source)
        assert result["outcome"] == "written" and result["source_id"] is None

    asyncio.run(scenario())


def test_memory_writer_guides_text_proposal_then_records_actual_document():
    """第一次只返回文本提议时，指导一次后通过真实工具写入并返回结果。"""

    async def scenario():
        store = InMemoryStore()
        body = "# 实际记忆\n\n已记录方案B，旧方案A已失效。\n"
        model = ToolModel(
            responses=[
                AIMessage(content='{"name":"write_file","parameters":{}}'),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": "/memories/daily/2026-10-14.md",
                                "content": body,
                            },
                            "id": "write",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {
                                "file_path": "/memories/daily/2026-10-14.md",
                            },
                            "id": "read",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "MemoryOutcome",
                            "args": {
                                "outcome": "candidate",
                            },
                            "id": "outcome",
                        }
                    ],
                ),
            ]
        )
        memory = PMMemory(store, model)
        source = await memory.capture("guided", "thread", [], purpose="方案更正")
        source["destination"] = "/memories/daily/2026-10-14.md"
        result = await memory.write(source)
        assert result["outcome"] == "candidate"
        assert await read_document(store, "guided", source["destination"]) == body
        assert any(
            "上轮没有有效的结构化结果" in message.text for message in model.seen[1]
        )
        rows = await memory_candidates(store, "guided", "方案B")
        assert rows[0]["source_refs"] == [source["bookmark"]]

    asyncio.run(scenario())


def test_memory_writer_rejects_persistent_text_proposals_after_one_guidance():
    """模型持续不调用工具时明确失败，不把文本当作已保存的记忆。"""

    async def scenario():
        store = InMemoryStore()
        model = ToolModel(
            responses=[AIMessage(content="准备记录"), AIMessage(content="准备调用工具")]
        )
        memory = PMMemory(store, model)
        source = await memory.capture("invalid", "thread", [], purpose="协议失败")
        with pytest.raises(RuntimeError, match="未返回有效 MemoryOutcome"):
            await memory.write(source)
        assert len(model.seen) == 2
        with pytest.raises(FileNotFoundError):
            await read_document(store, "invalid", source["destination"])

    asyncio.run(scenario())


def test_memory_writer_returns_format_guidance_before_saving_single_line_markdown():
    """错误格式不入库，模型收到指导后用真实工具写入多行 Markdown。"""

    async def scenario():
        store = InMemoryStore()
        body = "# 实际记忆\n\n方案A已失效，现在采用方案B。\n"
        path = "/memories/daily/2026-10-14.md"
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": path,
                                "content": "# 记忆摘要正文全部连在一行",
                            },
                            "id": "bad",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": path,
                                "content": body,
                            },
                            "id": "good",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "MemoryOutcome",
                            "args": {
                                "outcome": "candidate",
                            },
                            "id": "outcome",
                        }
                    ],
                ),
            ]
        )
        memory = PMMemory(store, model)
        source = await memory.capture("format", "thread", [], purpose="格式验证")
        source["destination"] = path
        await memory.write(source)
        assert await read_document(store, "format", path) == body
        assert any(
            isinstance(message, ToolMessage)
            and message.status == "error"
            and "写入未执行" in message.text
            for message in model.seen[1]
        )

    asyncio.run(scenario())


def test_memory_writer_accepts_only_verified_new_document_as_candidate_when_report_missing():
    """模型实际写入有来源的新文档后，即使漏报结果，也只能确认候选而非长期事实。"""

    async def scenario():
        store = InMemoryStore()
        memory = PMMemory(store)
        source = await memory.capture("verified", "thread", [], purpose="写入验证")
        body = "# 候选记忆\n\n新的观察，来源：" + source["bookmark"] + "\n"
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": source["destination"],
                                "content": body,
                            },
                            "id": "write",
                        }
                    ],
                ),
                AIMessage(content="MemoryOutcome candidate"),
                AIMessage(content="MemoryOutcome candidate"),
            ]
        )
        memory.model = model
        result = await memory.write(source)
        assert result["outcome"] == "candidate"
        assert result["outcome_origin"] == "verified_document"
        assert await read_document(store, "verified", source["destination"]) == body
        record = await memory.source("verified", source["id"])
        assert record["outcome_origin"] == "verified_document"
        rows = await memory_candidates(store, "verified", "新的观察")
        assert rows[0]["source_refs"] == [source["bookmark"]]

    asyncio.run(scenario())


def test_handoff_guides_early_ignore_report_until_document_is_written():
    """交接不能只报告 ignore；模型收到指导后必须保存实际文件。"""

    async def scenario():
        store = InMemoryStore()
        memory = PMMemory(store)
        source = await memory.capture(
            "handoff-guided", "thread", [], purpose="压缩前交接", handoff=True
        )
        body = "# 交接\n\n下一步验证重启，来源：" + source["bookmark"] + "\n"
        memory.model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "MemoryOutcome",
                            "args": {"outcome": "ignore"},
                            "id": "early",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": source["destination"],
                                "content": body,
                            },
                            "id": "write",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "MemoryOutcome",
                            "args": {"outcome": "candidate"},
                            "id": "done",
                        }
                    ],
                ),
            ]
        )
        await memory.write(source)
        assert (
            await read_document(store, "handoff-guided", source["destination"]) == body
        )
        assert (await memory.source("handoff-guided", source["id"]))[
            "handoff_ready"
        ] is True

    asyncio.run(scenario())


def test_consolidation_accepts_topic_only_artifact_without_result_report():
    """真实主题工具写入即使漏报结果，也可按来源校验为候选并索引。"""

    async def scenario():
        store = InMemoryStore()
        memory = PMMemory(store)
        source = await memory.capture("topic", "maintenance", [], purpose="整理")
        source["consolidation"] = True
        path = "/memories/topics/排队.md"
        body = "# 排队经验\n\n使用官方排队，来源：" + source["bookmark"]
        memory.model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": path,
                                "content": body,
                            },
                            "id": "write",
                        }
                    ],
                ),
                AIMessage(content="准备报告 candidate"),
                AIMessage(content="candidate"),
            ]
        )
        result = await memory.write(source)
        assert result["outcome_origin"] == "verified_document"
        assert (await memory_candidates(store, "topic", "排队经验"))[0]["path"] == path
        with pytest.raises(FileNotFoundError):
            await read_document(store, "topic", source["destination"])

    asyncio.run(scenario())


def test_consolidation_skips_only_unchanged_successful_inputs():
    """成功整理才生成凭据；来源、观察、状态和文档变化均重新调用模型。"""

    async def scenario():
        store = InMemoryStore()
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "MemoryOutcome",
                            "args": {"outcome": "ignore"},
                            "id": "done",
                        }
                    ],
                )
            ]
        )
        memory = PMMemory(store, model)
        await memory.consolidate("receipt")
        assert len(model.seen) == 1
        result = await memory.consolidate("receipt")
        assert result["outcome_origin"] == "unchanged_inputs" and len(model.seen) == 1
        sources = await store.asearch(memory.namespace("receipt"))
        assert len(sources) == 1
        await memory.capture("receipt", "pm", [], purpose="新来源")
        await memory.consolidate("receipt")
        assert len(model.seen) == 2
        await memory.observe("receipt", "pm", [HumanMessage(content="新决定")])
        await memory.consolidate("receipt")
        assert len(model.seen) == 3
        await ProjectStore(store).save(ProjectSnapshot("receipt", "新目标", (), ()))
        await memory.consolidate("receipt")
        assert len(model.seen) == 4
        await memory_files(store, "receipt").awrite(
            "/topics/new.md", "# 新主题\n新依据"
        )
        await memory.consolidate("receipt")
        assert len(model.seen) == 5

    asyncio.run(scenario())


def test_failed_consolidation_never_becomes_verified_noop():
    """缺失成果的失败不能因为输入相同而在下次变成成功。"""

    async def scenario():
        store = InMemoryStore()
        model = ToolModel(responses=[AIMessage(content="未调用工具")])
        memory = PMMemory(store, model)
        for _ in range(2):
            with pytest.raises(RuntimeError, match="未返回有效 MemoryOutcome"):
                await memory.consolidate("failed")
        assert len(model.seen) == 4
        assert (
            await store.aget(
                ("deep_loopminder", "pm_memory_consolidations", "failed"), "latest"
            )
            is None
        )

    asyncio.run(scenario())


def test_consolidation_does_not_consume_evidence_arriving_during_write():
    """整理过程中到达的新来源必须留给下一次处理。"""

    async def scenario():
        store = InMemoryStore()
        memory = PMMemory(store)

        async def write(source):
            await memory.capture("race", "pm", [], purpose="执行中到达的新来源")
            return {"outcome": "ignore", "outcome_origin": "model"}

        memory.write = AsyncMock(side_effect=write)
        await memory.consolidate("race")
        assert (
            await store.aget(
                ("deep_loopminder", "pm_memory_consolidations", "race"), "latest"
            )
            is None
        )
        memory.write = AsyncMock(return_value={"outcome": "ignore"})
        await memory.consolidate("race")
        memory.write.assert_awaited_once()

    asyncio.run(scenario())


def test_consolidation_guidance_allows_explicit_ignore_without_forcing_a_write():
    """结果协议缺失时，整理可以明确确认无新结论，无需编造正文。"""

    async def scenario():
        store = InMemoryStore()
        model = ToolModel(
            responses=[
                AIMessage(content="没有新结论"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "MemoryOutcome",
                            "args": {"outcome": "ignore"},
                            "id": "ignored",
                        }
                    ],
                ),
            ]
        )
        memory = PMMemory(store, model)
        result = await memory.consolidate("guided-ignore")
        assert result["outcome"] == "ignore" and result["outcome_origin"] == "model"
        assert any("没有新增结论" in message.text for message in model.seen[-1])
        assert (await memory.consolidate("guided-ignore"))[
            "outcome_origin"
        ] == "unchanged_inputs"
        assert len(model.seen) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "has_source,retry", [(False, False), (True, False), (True, True)]
)
def test_memory_status_reference_errors_are_recoverable_tool_results(has_source, retry):
    """来源不存在或尚无 Run 时，官方错误结果允许 PM 继续答复，不重提任务。"""

    async def scenario():
        store = InMemoryStore()
        runs = SimpleNamespace(get=AsyncMock(), create=AsyncMock())
        memory = PMMemory(store, client=SimpleNamespace(runs=runs))
        if has_source:
            await memory.capture(
                "status-boundary",
                "pm",
                [],
                purpose="仅保存原始来源",
                source_id="source-only",
            )
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "pm_memory_status",
                            "args": {"job_id": "source-only", "retry": retry},
                            "id": "query",
                        }
                    ],
                ),
                AIMessage(content="当前无法查询，先核对引用，不重复提交。"),
            ]
        )
        graph = create_agent(
            model, middleware=[PMAgentMemoryMiddleware(memory)], store=store
        )
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="查询记忆状态")]},
            {"configurable": {"project_id": "status-boundary", "thread_id": "pm"}},
        )
        error = next(
            message
            for message in result["messages"]
            if isinstance(message, ToolMessage)
        )
        assert error.status == "error"
        assert ("尚未关联官方 Run" if has_source else "未找到") in error.text
        assert result["messages"][-1].text == "当前无法查询，先核对引用，不重复提交。"
        runs.get.assert_not_awaited()
        runs.create.assert_not_awaited()
        if has_source:
            source = await memory.source("status-boundary", "source-only")
            assert source["purpose"] == "仅保存原始来源" and "status" not in source

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "name,args",
    [
        ("pm_read_memory", {"path": "/memories/../foreign/secret.md"}),
        ("pm_read_memory", {"path": "/memories/topics/valid.md", "offset": -1}),
        ("pm_read_memory", {"path": "/memories/topics/valid.md", "limit": 201}),
        ("pm_read_memory", {"path": "/memories/topics/missing.md"}),
        ("pm_read_bookmark", {"job_id": "foreign-source"}),
        ("pm_read_bookmark", {"job_id": "foreign-source", "offset": -1}),
    ],
)
def test_memory_read_boundaries_return_errors_and_allow_corrected_read(name, args):
    """非法读取可恢复，后续正确读取仍可执行，外项目来源不泄漏。"""

    async def scenario():
        store = InMemoryStore()
        memory = PMMemory(store)
        await memory_files(store, "reader").awrite(
            "/topics/valid.md", "# 当前项目\n合法资料"
        )
        await memory.capture(
            "foreign",
            "other",
            [HumanMessage(content="FOREIGN_PRIVATE_MARKER")],
            purpose="其它项目来源",
            source_id="foreign-source",
        )
        model = ToolModel(
            responses=[
                AIMessage(
                    content="", tool_calls=[{"name": name, "args": args, "id": "bad"}]
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "pm_read_memory",
                            "args": {"path": "/memories/topics/valid.md"},
                            "id": "corrected",
                        }
                    ],
                ),
                AIMessage(content="读取已恢复。"),
            ]
        )
        graph = create_agent(
            model, middleware=[PMAgentMemoryMiddleware(memory)], store=store
        )
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="测试读取边界")]},
            {"configurable": {"project_id": "reader", "thread_id": "pm"}},
        )
        outputs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert [m.status for m in outputs] == ["error", "success"]
        assert "合法资料" in outputs[1].text
        assert "FOREIGN_PRIVATE_MARKER" not in "".join(
            m.text for m in result["messages"]
        )
        assert result["messages"][-1].text == "读取已恢复。"

    asyncio.run(scenario())


def test_memory_read_infrastructure_failure_still_fails_run(monkeypatch):
    """只恢复输入/缺失资源错误，连接故障不能伪装为可用数据。"""

    async def scenario():
        store = InMemoryStore()
        memory = PMMemory(store)
        from src.middlewares.memory.pm_agent_memory import middleware as module

        monkeypatch.setattr(
            module,
            "read_document",
            AsyncMock(side_effect=ConnectionError("测试连接故障")),
        )
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "pm_read_memory",
                            "args": {"path": "/memories/topics/valid.md"},
                            "id": "read",
                        }
                    ],
                )
            ]
        )
        graph = create_agent(
            model, middleware=[PMAgentMemoryMiddleware(memory)], store=store
        )
        with pytest.raises(ConnectionError, match="测试连接故障"):
            await graph.ainvoke(
                {"messages": [HumanMessage(content="读取")]},
                {"configurable": {"project_id": "reader", "thread_id": "pm"}},
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "args",
    [
        {"query": "   "},
        {"query": "合法资料", "limit": 0},
        {"query": "合法资料", "limit": 21},
    ],
    ids=["blank-query", "limit-too-small", "limit-too-large"],
)
def test_memory_search_input_errors_allow_corrected_search(args):
    """搜索输入错误返回官方错误结果，修正参数后仍可找到实际资料。"""

    async def scenario():
        store = InMemoryStore()
        await memory_files(store, "searcher").awrite(
            "/topics/valid.md", "# 资料\n合法资料"
        )
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "memory_search", "args": args, "id": "bad"}],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "memory_search",
                            "args": {"query": "合法资料"},
                            "id": "fixed",
                        }
                    ],
                ),
                AIMessage(content="搜索已恢复。"),
            ]
        )
        graph = create_agent(model, middleware=[PMAgentMemoryMiddleware()], store=store)
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="测试搜索边界")]},
            {"configurable": {"project_id": "searcher", "thread_id": "pm"}},
        )
        outputs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert [m.status for m in outputs] == ["error", "success"]
        assert "/memories/topics/valid.md" in outputs[1].text
        assert result["messages"][-1].text == "搜索已恢复。"

    asyncio.run(scenario())


def test_memory_search_infrastructure_failure_still_fails_run(monkeypatch):
    """搜索后端连接失败仍交给官方Run，不冒充无匹配或可恢复参数错误。"""

    async def scenario():
        from src.middlewares.memory.pm_agent_memory import middleware as module

        store = InMemoryStore()
        monkeypatch.setattr(
            module,
            "memory_candidates",
            AsyncMock(side_effect=ConnectionError("测试搜索连接故障")),
        )
        model = ToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "memory_search",
                            "args": {"query": "合法资料"},
                            "id": "search",
                        }
                    ],
                )
            ]
        )
        graph = create_agent(model, middleware=[PMAgentMemoryMiddleware()], store=store)
        with pytest.raises(ConnectionError, match="测试搜索连接故障"):
            await graph.ainvoke(
                {"messages": [HumanMessage(content="搜索")]},
                {"configurable": {"project_id": "searcher", "thread_id": "pm"}},
            )

    asyncio.run(scenario())
