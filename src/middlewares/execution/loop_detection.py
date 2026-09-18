"""Bounded call-pattern loop detection for Issue #14.

This is deliberately a pure policy primitive. It does not count total calls,
inspect tool results, mirror subagent lifecycle state, or persist history.
Official LangChain call limits and Deep Agents subagent lifecycle remain the
authoritative mechanisms for those concerns.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence


class LoopAction(str, Enum):
    ALLOW = "allow"
    WARN = "warn"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class CallSignature:
    """Deterministic identity for one observed invocation pattern."""

    kind: str
    target: str
    objective: str = ""
    phase: str = ""

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("kind must be non-empty")
        if not self.target.strip():
            raise ValueError("target must be non-empty")


@dataclass(frozen=True, slots=True)
class LoopDecision:
    action: LoopAction
    period: int | None = None
    repetitions: int = 0
    reason: str | None = None


def normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def tool_signature(
    tool_name: str,
    *,
    resource: str = "",
    operation: str = "",
    phase: str = "",
) -> CallSignature:
    """Build a stable signature from already-canonicalized business metadata.

    Callers remain responsible for provider-specific canonicalization such as
    read range buckets or command fingerprints. This module intentionally does
    not parse tool arguments or results.
    """

    target = "|".join(
        part for part in (normalize_text(tool_name), normalize_text(resource), normalize_text(operation))
        if part
    )
    return CallSignature("tool", target, phase=normalize_text(phase))


def delegation_signature(
    subagent_type: str,
    *,
    objective: str,
    asynchronous: bool = False,
    phase: str = "",
) -> CallSignature:
    """Represent sync/async delegation without inventing lifecycle identity."""

    return CallSignature(
        "async_task" if asynchronous else "sync_task",
        normalize_text(subagent_type),
        objective=normalize_text(objective),
        phase=normalize_text(phase),
    )


def detect_loop(
    history: Sequence[CallSignature] | Iterable[CallSignature],
    *,
    max_period: int = 3,
    warn_repetitions: int = 2,
    stop_repetitions: int = 3,
) -> LoopDecision:
    """Detect a repeated suffix pattern in a bounded caller-provided window.

    The newest suffix is checked for periods 1..max_period. A pattern is only
    equivalent when the full signature matches, including objective and phase,
    so legitimate rework in a changed phase is not blocked solely because the
    same agent/tool name appears again.
    """

    if max_period < 1:
        raise ValueError("max_period must be >= 1")
    if warn_repetitions < 2:
        raise ValueError("warn_repetitions must be >= 2")
    if stop_repetitions < warn_repetitions:
        raise ValueError("stop_repetitions must be >= warn_repetitions")

    items = tuple(history)
    if not items:
        return LoopDecision(LoopAction.ALLOW)

    best_period: int | None = None
    best_repetitions = 1

    for period in range(1, min(max_period, len(items)) + 1):
        block = items[-period:]
        repetitions = 1
        cursor = len(items) - period
        while cursor >= period and items[cursor - period:cursor] == block:
            repetitions += 1
            cursor -= period

        if repetitions > best_repetitions:
            best_period = period
            best_repetitions = repetitions

    if best_period is None or best_repetitions < warn_repetitions:
        return LoopDecision(LoopAction.ALLOW)

    reason = (
        "delegation_loop_capped"
        if items[-1].kind in {"sync_task", "async_task"}
        else "loop_capped"
    )
    action = (
        LoopAction.STOP
        if best_repetitions >= stop_repetitions
        else LoopAction.WARN
    )
    return LoopDecision(action, best_period, best_repetitions, reason)
