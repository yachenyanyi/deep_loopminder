from src.runtime.pm import PMProfile, default_pm_profile


def test_default_pm_profile_is_project_control_only() -> None:
    profile = default_pm_profile()

    assert profile.role == "project_manager"
    assert "project.command" in profile.capabilities
    assert "project.delegate.role_requirement" in profile.capabilities
    assert "repo.write.source" not in profile.capabilities
    assert "shell.execute" not in profile.capabilities
    assert "test.execute" not in profile.capabilities


def test_pm_profile_declares_reviewed_owner_requirements() -> None:
    profile = default_pm_profile()

    assert profile.approval_policy == "human_boundary"
    assert profile.memory_policy == "pm_project_orientation_and_recall"
    assert profile.middleware_profile == (
        "HumanDecisionRequestMiddleware",
        "human_decision_hitl_middleware",
        "PMAgentMemoryMiddleware",
    )


def test_pm_profile_does_not_bind_provider_or_session_control() -> None:
    profile = default_pm_profile()

    assert "provider.select" not in profile.capabilities
    assert "provider.session.control" not in profile.capabilities


def test_pm_profile_is_immutable_declaration() -> None:
    profile = default_pm_profile()

    assert isinstance(profile, PMProfile)
    assert isinstance(profile.capabilities, frozenset)
