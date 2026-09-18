"""Pure policy primitives for reconciling external mutation outcomes.

This module deliberately does not implement provider calls, retries, checkpoints,
leases, workflow replay, or sandbox lifecycle.  Those remain owned by the
provider and the official LangGraph / Deep Agents runtime facilities described
in issue #24.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OperationOutcome(str, Enum):
    """Durable knowledge about one logical external mutation."""

    NOT_STARTED = "not_started"
    OUTCOME_UNKNOWN = "outcome_unknown"
    COMPLETED = "completed"
    FAILED = "failed"


class ReconcileAction(str, Enum):
    """Next safe action derived from durable facts and provider capabilities."""

    EXECUTE = "execute"
    LOOKUP = "lookup"
    REPLAY_COMPLETION = "replay_completion"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class OperationState:
    """Provider-neutral durable projection for one logical mutation.

    ``completion_ref`` is an opaque durable receipt/result reference.  It is not
    a LangGraph checkpoint, process handle, runtime generation, or liveness
    proof.  ``provider_operation_ref`` is likewise only a provider lookup key.
    """

    operation_id: str
    outcome: OperationOutcome = OperationOutcome.NOT_STARTED
    provider_operation_ref: str | None = None
    completion_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.operation_id:
            raise ValueError("operation_id must not be empty")
        if self.outcome is OperationOutcome.COMPLETED and not self.completion_ref:
            raise ValueError("completed operation requires completion_ref")
        if self.completion_ref and self.outcome is not OperationOutcome.COMPLETED:
            raise ValueError("completion_ref is only valid for completed operations")


def reconcile_action(
    state: OperationState,
    *,
    provider_lookup_available: bool,
    provider_idempotency_available: bool,
) -> ReconcileAction:
    """Choose the next safe action without performing runtime lifecycle work.

    Crash-window semantics:
    - NOT_STARTED: no external call was attempted, so execution may start.
    - OUTCOME_UNKNOWN: never blindly retry. Prefer provider lookup; when lookup
      is unavailable, retry is only admissible if the provider guarantees a
      stable idempotency primitive for the same logical operation.
    - COMPLETED: replay the durable completion instead of executing again.
    - FAILED: terminal provider-confirmed failure; higher policy may decide what
      a *new* logical operation should do, but this operation is not retried here.
    """

    if state.outcome is OperationOutcome.NOT_STARTED:
        return ReconcileAction.EXECUTE
    if state.outcome is OperationOutcome.COMPLETED:
        return ReconcileAction.REPLAY_COMPLETION
    if state.outcome is OperationOutcome.FAILED:
        return ReconcileAction.STOP

    if provider_lookup_available:
        return ReconcileAction.LOOKUP
    if provider_idempotency_available:
        return ReconcileAction.EXECUTE
    return ReconcileAction.STOP
