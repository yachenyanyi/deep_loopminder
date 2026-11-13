"""Tests for the stable PM system prompt contract."""

from src.deep_agents.agents.pm_agent.agent import stable_pm_system_prompt


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
        # task_id是工具契约字段名；禁止注入具体运行值，而非禁止说明工具参数。
        "task_id=",
        '"task_id":',
        "thread_id",
        "checkpoint_id",
        "provider_id",
    ):
        assert volatile_fact not in prompt
    # 工具名 begin_codex_review 属于固定契约，不是提供方映射；剔除后
    # 提示词不应再出现具体提供方名称或选择规则。
    for provider in ("claude", "codex"):
        assert provider not in prompt.replace("begin_codex_review", "")
