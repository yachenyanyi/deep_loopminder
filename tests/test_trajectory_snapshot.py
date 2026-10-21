"""轨迹请求快照：验证官方循环中的Schema、配置、用量和压缩来源。"""

import asyncio

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from test_trajectory import ToolModel

from src.middlewares.trajectory import TrajectoryMiddleware, TrajectoryRecorder
from src.middlewares.trajectory.web import read_events


def test_request_snapshot_has_call_time_schema_and_classifies_official_summary(
    tmp_path,
):
    async def scenario():
        @tool
        def lookup(customer: str) -> str:
            """查询客户状态。"""
            return customer

        model = ToolModel(
            responses=[
                AIMessage(
                    content="完成",
                    usage_metadata={
                        "input_tokens": 19,
                        "output_tokens": 4,
                        "total_tokens": 23,
                        "input_token_details": {"cache_read": 8},
                        "output_token_details": {"reasoning": 2},
                    },
                )
            ]
        )
        recorder = TrajectoryRecorder(tmp_path)

        class InjectContext(AgentMiddleware):
            async def awrap_model_call(self, request, handler):
                return await handler(
                    request.override(
                        system_message=SystemMessage(content="注入记忆后的真实请求")
                    )
                )

        observer = TrajectoryMiddleware(recorder)
        graph = create_agent(
            model,
            tools=[lookup],
            system_prompt="管理项目",
            middleware=[observer, InjectContext(), observer.request_capture()],
        )
        await graph.ainvoke(
            {
                "messages": [
                    HumanMessage(
                        content="历史摘要",
                        additional_kwargs={"lc_source": "summarization"},
                    ),
                    HumanMessage(content="客户请求"),
                ]
            },
            {"configurable": {"thread_id": "snapshot"}},
        )
        rows = read_events(recorder.path)["events"]
        headers = [e for e in rows if e["kind"] == "context"]
        assert headers[0]["data"]["system"]["content"] == "管理项目"
        header = headers[-1]
        assert header["data"]["system"]["content"] == "注入记忆后的真实请求"
        schema = header["data"]["tools"][0]["function"]
        assert schema["name"] == "lookup"
        assert schema["parameters"]["properties"]["customer"]["type"] == "string"
        assert len(header["data"]["messages"]) == 2
        assert header["data"]["snapshot_scope"] == "model_request"
        assert sum(e["kind"] == "input" for e in rows) == 1
        assert sum(e["kind"] == "compaction" for e in rows) == 1
        assert [e["phase"] for e in rows if e["kind"] == "turn"] == ["start", "end"]
        response = next(e for e in rows if e["kind"] == "model" and e["phase"] == "end")
        assert response["span_id"] == header["span_id"]
        assert (
            response["data"]["messages"][0]["usage_metadata"]["output_token_details"][
                "reasoning"
            ]
            == 2
        )
        assert [e["seq"] for e in rows] == list(range(1, len(rows) + 1))

    asyncio.run(scenario())
