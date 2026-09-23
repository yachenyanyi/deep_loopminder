import pytest
from langchain.agents.middleware import HumanInTheLoopMiddleware

from src.middlewares.approval import HumanDecisionRequestMiddleware
from src.middlewares.memory.pm_agent_memory import PMAgentMemoryMiddleware
from src.runtime.middleware_assembly import assemble_pm_user_middleware
from src.runtime.pm import PMProfile, default_pm_profile


def test_pm_assembly_resolves_reviewed_owner_middleware() -> None:
    middleware = assemble_pm_user_middleware(default_pm_profile())

    assert len(middleware) == 4
    assert isinstance(middleware[1], HumanDecisionRequestMiddleware)
    assert isinstance(middleware[2], HumanInTheLoopMiddleware)
    assert isinstance(middleware[3], PMAgentMemoryMiddleware)


def test_pm_profile_declares_pm_memory_policy_without_execution_capability() -> None:
    profile = default_pm_profile()

    assert profile.memory_policy == "pm_project_orientation_and_recall"
    assert "PMAgentMemoryMiddleware" in profile.middleware_profile
    assert "shell.execute" not in profile.capabilities
    assert "repo.write.source" not in profile.capabilities


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


def test_pm_assembly_fails_closed_for_unreviewed_context_policy() -> None:
    profile = default_pm_profile()
    unreviewed = PMProfile(
        role=profile.role,
        capabilities=profile.capabilities,
        context_policy="custom_prompt_engine",
        memory_policy=profile.memory_policy,
        budget_policy=profile.budget_policy,
        approval_policy=profile.approval_policy,
        middleware_profile=profile.middleware_profile,
    )

    with pytest.raises(ValueError, match="#33 projection path"):
        assemble_pm_user_middleware(unreviewed)
