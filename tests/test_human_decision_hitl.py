import pytest
from langchain.agents.middleware import HumanInTheLoopMiddleware

from src.middlewares.approval.human_decision import (
    HUMAN_DECISION_TOOL_NAME,
    HumanDecisionRequestMiddleware,
    human_decision_hitl_middleware,
    human_decision_interrupt_on,
)


def test_human_decision_tool_is_owned_by_approval_middleware() -> None:
    middleware = HumanDecisionRequestMiddleware()

    assert [tool.name for tool in middleware.tools] == [HUMAN_DECISION_TOOL_NAME]


def test_human_decision_tool_fails_closed_without_official_hitl() -> None:
    tool = HumanDecisionRequestMiddleware().tools[0]

    with pytest.raises(RuntimeError, match="must be intercepted"):
        tool.invoke({"question": "Approve architecture change?", "context": "ADR-42"})


def test_human_decision_uses_official_respond_decision() -> None:
    interrupt_on = human_decision_interrupt_on()
    config = interrupt_on[HUMAN_DECISION_TOOL_NAME]

    assert isinstance(config, dict)
    assert config["allowed_decisions"] == ["respond"]
    assert "exceeds the agent's authority" in config["description"]


def test_human_decision_builds_official_hitl_middleware() -> None:
    middleware = human_decision_hitl_middleware()

    assert isinstance(middleware, HumanInTheLoopMiddleware)
