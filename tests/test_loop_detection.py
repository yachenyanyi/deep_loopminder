import pytest

from src.middlewares.execution.loop_detection import (
    LoopAction,
    delegation_signature,
    detect_loop,
    tool_signature,
)


def test_identical_tool_calls_warn_then_stop():
    call = tool_signature("search", operation="same query")
    assert detect_loop([call, call]).action is LoopAction.WARN
    decision = detect_loop([call, call, call])
    assert decision.action is LoopAction.STOP
    assert decision.period == 1
    assert decision.reason == "loop_capped"


def test_alternating_tool_pattern_is_detected():
    a = tool_signature("read_file", resource="a.py", operation="0:100")
    b = tool_signature("read_file", resource="b.py", operation="0:100")
    decision = detect_loop([a, b, a, b, a, b])
    assert decision.action is LoopAction.STOP
    assert decision.period == 2


def test_pagination_is_not_collapsed_into_same_signature():
    calls = [
        tool_signature("read_file", resource="a.py", operation=f"page:{page}")
        for page in range(1, 5)
    ]
    assert detect_loop(calls).action is LoopAction.ALLOW


def test_sync_delegation_loop_uses_no_synthetic_task_identity():
    a = delegation_signature("researcher", objective="investigate x")
    b = delegation_signature("reviewer", objective="investigate x")
    decision = detect_loop([a, b, a, b, a, b])
    assert decision.action is LoopAction.STOP
    assert decision.reason == "delegation_loop_capped"
    assert a.kind == "sync_task"


def test_async_delegation_pattern_is_separate_from_sync_lifecycle():
    a = delegation_signature("researcher", objective="investigate x", asynchronous=True)
    b = delegation_signature("reviewer", objective="investigate x", asynchronous=True)
    decision = detect_loop([a, b, a, b])
    assert decision.action is LoopAction.WARN
    assert decision.reason == "delegation_loop_capped"
    assert a.kind == "async_task"


def test_changed_phase_allows_legitimate_rework_edge():
    history = [
        delegation_signature("researcher", objective="x", phase="research"),
        delegation_signature("reviewer", objective="x", phase="review"),
        delegation_signature("researcher", objective="x", phase="rework"),
        delegation_signature("reviewer", objective="x", phase="verify"),
    ]
    assert detect_loop(history).action is LoopAction.ALLOW


def test_changed_objective_does_not_count_as_same_delegation():
    history = [
        delegation_signature("researcher", objective="x"),
        delegation_signature("researcher", objective="y"),
        delegation_signature("researcher", objective="z"),
    ]
    assert detect_loop(history).action is LoopAction.ALLOW


def test_normalization_is_stable_for_case_and_whitespace():
    a = tool_signature(" Search ", operation="  Foo   BAR ")
    b = tool_signature("search", operation="foo bar")
    assert a == b


def test_invalid_thresholds_fail_closed_at_configuration_time():
    with pytest.raises(ValueError):
        detect_loop([], max_period=0)
    with pytest.raises(ValueError):
        detect_loop([], warn_repetitions=1)
    with pytest.raises(ValueError):
        detect_loop([], warn_repetitions=3, stop_repetitions=2)
