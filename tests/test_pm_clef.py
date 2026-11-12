"""Clef's decision is accepted only from a valid structured response."""

import asyncio

import httpx
import pytest

from src.deep_agents.agents.pm_agent.clef import judge_discussion


@pytest.mark.parametrize("choice", ["pass", "reject", "unknown"])
def test_clef_uses_typed_choice_and_returns_provider_answer(choice):
    async def scenario():
        async def handler(request):
            assert request.url.host == "api.cloudflare.com"
            assert request.headers["Authorization"] == "Bearer test-token"
            body = __import__("json").loads(request.content)
            assert body["questions"]["development_readiness"]["type"] == "choice"
            return httpx.Response(200, json={"success": True, "result": {"answers": {"development_readiness": {"choice": choice}}}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            answer = await judge_discussion({"task": "example"}, account_id="abc123", token="test-token", client=client)
        assert answer.outcome == choice
        assert answer.answer["choice"] == choice

    asyncio.run(scenario())


def test_clef_fails_closed_on_missing_or_malformed_result():
    async def scenario():
        async def handler(request):
            return httpx.Response(200, json={"success": True, "result": {}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await judge_discussion({"task": "example"}, account_id="abc123", token="secret", client=client)
        assert result.outcome == "unknown"
        assert result.error == "clef_invalid_response"
        assert "secret" not in repr(result)
        assert (await judge_discussion({}, account_id="abc", token="secret")).outcome == "unknown"

    asyncio.run(scenario())
