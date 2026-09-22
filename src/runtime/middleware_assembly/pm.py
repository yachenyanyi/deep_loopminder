"""Resolve PM profile requirements to reviewed user middleware instances."""

from langchain.agents.middleware import AgentMiddleware

from ...middlewares.approval import (
    HumanDecisionRequestMiddleware,
    human_decision_hitl_middleware,
)
from ...middlewares.memory.pm_agent_memory import PMAgentMemoryMiddleware
from ..pm import PMProfile

_HUMAN_DECISION_REQUEST = "HumanDecisionRequestMiddleware"
_HUMAN_DECISION_HITL = "human_decision_hitl_middleware"
_PM_MEMORY = "PMAgentMemoryMiddleware"
_REVIEWED_PM_REQUIREMENTS = (
    _HUMAN_DECISION_REQUEST,
    _HUMAN_DECISION_HITL,
    _PM_MEMORY,
)


def assemble_pm_user_middleware(profile: PMProfile) -> tuple[AgentMiddleware, ...]:
    """Resolve reviewed PM requirements without replacing official runtime."""
    if profile.middleware_profile != _REVIEWED_PM_REQUIREMENTS:
        raise ValueError(
            "PM middleware_profile must exactly match the reviewed PM requirements"
        )

    return (
        HumanDecisionRequestMiddleware(),
        human_decision_hitl_middleware(),
        PMAgentMemoryMiddleware(),
    )
