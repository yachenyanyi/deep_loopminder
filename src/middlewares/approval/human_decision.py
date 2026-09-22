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
    """Request a human decision for an authority boundary the agent cannot cross."""
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
                "A project decision exceeds the agent's authority. "
                "Provide the human decision that should guide the project."
            ),
        }
    }


def human_decision_hitl_middleware() -> HumanInTheLoopMiddleware:
    """Build the official HITL middleware for project decision requests."""
    return HumanInTheLoopMiddleware(interrupt_on=human_decision_interrupt_on())
