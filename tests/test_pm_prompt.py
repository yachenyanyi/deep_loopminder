"""Tests for the stable PM system prompt contract."""

from src.runtime.pm.prompt import stable_pm_system_prompt


def test_stable_prompt_contains_project_control_invariants() -> None:
    prompt = stable_pm_system_prompt()

    assert "human goal as the outcome" in prompt
    assert "Prefer evidence over status claims" in prompt
    assert "known facts, assumptions, and unknowns" in prompt
    assert "Plans serve the goal" in prompt
    assert "greatest effect on reaching the goal" in prompt
    assert "Runtime Project State is the source of truth" in prompt
    assert "role and capability" in prompt
    assert "Worker execution completion is not task validation" in prompt
    assert "typed ProjectCommand" in prompt
    assert "Human Gate" in prompt
    assert "Never weaken acceptance criteria" in prompt


def test_stable_prompt_excludes_volatile_runtime_and_provider_mapping() -> None:
    prompt = stable_pm_system_prompt().lower()

    for volatile_fact in (
        "project_id",
        "task_id",
        "thread_id",
        "checkpoint_id",
        "provider_id",
        "claude",
        "codex",
    ):
        assert volatile_fact not in prompt
