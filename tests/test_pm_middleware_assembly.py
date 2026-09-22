import pytest
from langchain.agents.middleware import HumanInTheLoopMiddleware

from src.middlewares.approval import HumanDecisionRequestMiddleware
from src.runtime.middleware_assembly import assemble_pm_user_middleware
from src.runtime.pm import PMProfile, default_pm_profile


def test_pm_assembly_resolves_reviewed_human_decision_pair() -> None:
    middleware = assemble_pm_user_middleware(default_pm_profile())

    assert len(middleware) == 2
    assert isinstance(middleware[0], HumanDecisionRequestMiddleware)
    assert isinstance(middleware[1], HumanInTheLoopMiddleware)


def test_pm_assembly_fails_closed_for_unreviewed_requirement() -> None:
    profile = default_pm_profile()
    unreviewed = PMProfile(
        role=profile.role,
        capabilities=profile.capabilities,
        context_policy=profile.context_policy,
        memory_policy=profile.memory_policy,
        budget_policy=profile.budget_policy,
        approval_policy=profile.approval_policy,
        middleware_profile=("UnknownMiddleware",),
    )

    with pytest.raises(ValueError, match="exactly match the reviewed"):
        assemble_pm_user_middleware(unreviewed)
