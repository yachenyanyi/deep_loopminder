import pytest
from langchain.agents.middleware import HumanInTheLoopMiddleware

from src.deep_agents.agents.pm_agent import PMProfile, default_pm_profile
from src.deep_agents.agents.pm_agent.agent import assemble_pm_user_middleware
from src.middlewares.memory.pm_agent_memory import PMAgentMemoryMiddleware
from src.middlewares.project_state import ProjectStateMiddleware
from src.middlewares.approval import HumanDecisionRequestMiddleware
from src.middlewares.agent.user_configurable import PMModelSelectionMiddleware
from src.deep_agents.agents.pm_agent.discussion import PMDiscussionMiddleware


def test_pm_assembly_resolves_reviewed_owner_middleware() -> None:
    middleware = assemble_pm_user_middleware(default_pm_profile())

    # 固定提示词投影后依次是模型选择、商讨门禁、人工审批、项目状态与记忆。
    assert len(middleware) == 7
    assert isinstance(middleware[1], PMModelSelectionMiddleware)
    assert isinstance(middleware[2], PMDiscussionMiddleware)
    assert isinstance(middleware[3], HumanDecisionRequestMiddleware)
    assert isinstance(middleware[4], HumanInTheLoopMiddleware)
    assert isinstance(middleware[5], ProjectStateMiddleware)
    assert isinstance(middleware[6], PMAgentMemoryMiddleware)


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


def test_pm_assembly_explicitly_enables_role_based_acp_tools() -> None:
    from src.middlewares.acp import ACPMiddleware

    acp = ACPMiddleware(
        async_subagents=[
            {
                "name": "researcher",
                "description": "Read-only investigation",
                "graph_id": "acp_worker",
            }
        ]
    )
    profile = default_pm_profile(acp_enabled=True)
    assembled = assemble_pm_user_middleware(profile, acp=acp)
    assert assembled[-1] is acp
    assert [tool.name for tool in acp.tools] == [
        "spawn_agent",
        "followup_task",
        "send_message",
        "wait_agent",
        "list_agents",
        "interrupt_agent",
    ]
    assert "provider.select" not in profile.capabilities
    with pytest.raises(ValueError, match="exactly match the reviewed"):
        assemble_pm_user_middleware(default_pm_profile(), acp=acp)
