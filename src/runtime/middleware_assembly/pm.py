"""Resolve PM profile requirements to reviewed user middleware instances."""

from langchain.agents.middleware import AgentMiddleware

from ...middlewares.approval import (
    HumanDecisionRequestMiddleware,
    human_decision_hitl_middleware,
)
from ..pm import PMProfile

_HUMAN_DECISION_REQUEST = "HumanDecisionRequestMiddleware"
_HUMAN_DECISION_HITL = "human_decision_hitl_middleware"
_REVIEWED_PM_REQUIREMENTS = (_HUMAN_DECISION_REQUEST, _HUMAN_DECISION_HITL)


def assemble_pm_user_middleware(profile: PMProfile) -> tuple[AgentMiddleware, ...]:
    """Resolve the reviewed PM requirement pair without replacing official runtime."""
    if profile.middleware_profile != _REVIEWED_PM_REQUIREMENTS:
        raise ValueError(
            "PM middleware_profile must exactly match the reviewed human-decision pair"
        )

    return (
        HumanDecisionRequestMiddleware(),
        human_decision_hitl_middleware(),
    )
