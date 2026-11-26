"""Official HITL surface for project-level human decisions.

The Tool is owned by this middleware, while pause/resume and human response
handling remain entirely owned by LangChain HumanInTheLoopMiddleware and
LangGraph persistence.
"""

from langchain.agents.middleware import (
    AgentMiddleware,
    HumanInTheLoopMiddleware,
    InterruptOnConfig,
)
from langchain_core.tools import tool

HUMAN_DECISION_TOOL_NAME = "request_human_decision"


@tool(HUMAN_DECISION_TOOL_NAME)
def request_human_decision(question: str, context: str = "") -> str:
    """仅用于超出 PM 权限或必须由用户正式拍板的关键取舍。question 写待决定问题，context 写必要背景。调用后会话暂停，等待用户在界面答复。普通沟通提问直接文字回复，勿滥用。"""
    raise RuntimeError(
        "request_human_decision must be intercepted by HumanInTheLoopMiddleware"
    )


class HumanDecisionRequestMiddleware(AgentMiddleware):
    """Register the human-decision Tool without owning HITL lifecycle."""

    tools = (request_human_decision,)


def human_decision_interrupt_on() -> dict[str, bool | InterruptOnConfig]:
    """Return official HITL configuration for the human-decision Tool."""
    return {
        HUMAN_DECISION_TOOL_NAME: {
            "allowed_decisions": ["respond"],
            "description": (
                "当前项目决定超出 PM 权限，请给出指导项目继续推进的正式决定。"
            ),
        }
    }


def human_decision_hitl_middleware() -> HumanInTheLoopMiddleware:
    """Build the official HITL middleware for project decision requests."""
    return HumanInTheLoopMiddleware(interrupt_on=human_decision_interrupt_on())
