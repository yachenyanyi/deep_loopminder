"""Model discovery and configuration use a separate credential store."""

import asyncio

import httpx
import pytest
from langgraph.store.memory import InMemoryStore

from src.middlewares.agent.model_catalog import ModelCatalog


class FakeCredentials:
    def __init__(self):
        self.values = {}

    async def put(self, identity, secret):
        self.values[identity] = secret

    async def get(self, identity):
        return self.values.get(identity)


def test_catalog_keeps_secret_out_of_store_and_discovers_models():
    async def scenario():
        store, credentials = InMemoryStore(), FakeCredentials()
        catalog = ModelCatalog(store, credentials)
        value = await catalog.save("local", "http://127.0.0.1:8317/v1", token="secret-value", models=[{"id": "alpha", "enabled": True, "reasoning_levels": None, "token": "should-not-save"}])
        assert "secret" not in repr(value) and "should-not-save" not in repr(value)
        assert (await catalog.get("local")) == value

        async def handler(request):
            assert request.url.path == "/v1/models"
            assert request.headers["Authorization"] == "Bearer secret-value"
            return httpx.Response(200, json={"data": [{"id": "alpha"}, {"id": "beta"}, {"id": "alpha"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await catalog.discover("local", client=client) == ["alpha", "beta"]
        assert (await catalog.get("local"))["models"][0]["enabled"] is True

    asyncio.run(scenario())


def test_catalog_rejects_credentials_in_url_and_insecure_remote_http():
    async def scenario():
        catalog = ModelCatalog(InMemoryStore(), FakeCredentials())
        for url in ("http://example.com/v1", "https://user:pass@example.com/v1", "https://example.com/v1?token=secret"):
            with pytest.raises(ValueError):
                await catalog.save("unsafe", url)

    asyncio.run(scenario())


def test_pm_model_switch_retains_prompt_and_rejects_disabled_model(monkeypatch):
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from test_pm_memory_integration import ToolModel

    from src.middlewares.agent import model_catalog
    from src.middlewares.agent.user_configurable import PMModelSelectionMiddleware

    async def scenario():
        import langchain_openai

        store, credentials = InMemoryStore(), FakeCredentials()
        monkeypatch.setattr(model_catalog, "WindowsCredentials", lambda: credentials)
        selected = ToolModel(responses=[AIMessage(content="selected reply")])
        monkeypatch.setattr(langchain_openai, "ChatOpenAI", lambda **kwargs: selected)
        catalog = ModelCatalog(store, credentials)
        await catalog.save("local", "http://127.0.0.1:8317/v1", models=[{"id": "alpha", "enabled": True, "reasoning_levels": ["high"]}])
        default = ToolModel(responses=[AIMessage(content="default reply")])
        graph = create_agent(default, system_prompt="PM governance", middleware=[PMModelSelectionMiddleware()], store=store, checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "thread", "model_provider": "local", "model_name": "alpha", "reasoning_effort": "high"}}
        await graph.ainvoke({"messages": [("user", "hello")]}, config)
        first = str(selected.seen[-1][0].content)
        assert "PM governance" in first and "模型已切换" in first
        await graph.ainvoke({"messages": [("user", "again")]}, config)
        assert "模型已切换" not in str(selected.seen[-1][0].content)
        await catalog.save("local", "http://127.0.0.1:8317/v1", models=[{"id": "alpha", "enabled": False}])
        with pytest.raises(ValueError, match="已禁用"):
            await graph.ainvoke({"messages": [("user", "again")]}, config)
        assert not default.seen

    asyncio.run(scenario())
