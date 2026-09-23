"""Resolve PM profile requirements to reviewed user middleware instances."""

from langchain.agents.middleware import AgentMiddleware

from ...middlewares.approval import (
    HumanDecisionRequestMiddleware,
    human_decision_hitl_middleware,
)
from ...middlewares.context_projection import make_context_projection_prompt
from ...middlewares.memory.pm_agent_memory import PMAgentMemoryMiddleware
from ..pm import PMProfile, stable_pm_system_prompt

_HUMAN_DECISION_REQUEST = "HumanDecisionRequestMiddleware"
_HUMAN_DECISION_HITL = "human_decision_hitl_middleware"
_PM_MEMORY = "PMAgentMemoryMiddleware"
_PM_CONTEXT_POLICY = "pm_project_projection"
_REVIEWED_PM_REQUIREMENTS = (
    _HUMAN_DECISION_REQUEST,
    _HUMAN_DECISION_HITL,
    _PM_MEMORY,
)


def assemble_pm_user_middleware(profile: PMProfile) -> tuple[AgentMiddleware, ...]:
    """Resolve reviewed PM requirements without replacing official runtime."""
    if profile.context_policy != _PM_CONTEXT_POLICY:
        raise ValueError("PM context_policy must use the reviewed #33 projection path")
    if profile.middleware_profile != _REVIEWED_PM_REQUIREMENTS:
        raise ValueError(
            "PM middleware_profile must exactly match the reviewed PM requirements"
        )

    return (
        make_context_projection_prompt(stable_pm_system_prompt()),
        HumanDecisionRequestMiddleware(),
        human_decision_hitl_middleware(),
        PMAgentMemoryMiddleware(),
    )
