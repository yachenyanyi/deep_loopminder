"""Restart-safe reattach policy for ephemeral remote execution handles.

This module consumes fresh provider/runtime lookup facts only. It deliberately
does not persist process state or mirror LangGraph run, thread, checkpoint, or
stream lifecycle.
"""

from dataclasses import dataclass
from enum import StrEnum


class ReattachDecision(StrEnum):
    """Conservative action for a persisted execution handle after restart."""

    REATTACH = "reattach"
    RECONCILE_NEW_GENERATION = "reconcile_new_generation"
    PRESERVE_UNVERIFIABLE = "preserve_unverifiable"


@dataclass(frozen=True, slots=True)
class RestartReattachFact:
    """Fresh provider evidence for an execution referenced before host restart.

    ``persisted_handle`` is diagnostic identity only. A PID, pane id, process
    handle, native session id, or conversation id cannot establish liveness.
    ``execution_found`` must come from a fresh provider/runtime lookup.
    ``provider_guarantees_reattach`` must reflect the provider's documented
    execution continuity contract rather than graph/checkpoint continuity.
    """

    persisted_handle: str
    lookup_supported: bool
    execution_found: bool | None = None
    provider_guarantees_reattach: bool = False

    def __post_init__(self) -> None:
        """Require a non-empty diagnostic handle without granting it authority."""
        if not self.persisted_handle.strip():
            raise ValueError("persisted_handle must be non-empty")


def restart_reattach_decision(fact: RestartReattachFact) -> ReattachDecision:
    """Allow reattach only when fresh provider evidence proves continuity.

    A missing execution is safe to move toward reconciliation and a new
    generation. Unsupported or ambiguous lookup remains unverifiable: callers
    must preserve/reconcile rather than infer death and blindly relaunch.
    """
    if not fact.lookup_supported or fact.execution_found is None:
        return ReattachDecision.PRESERVE_UNVERIFIABLE
    if not fact.execution_found:
        return ReattachDecision.RECONCILE_NEW_GENERATION
    if fact.provider_guarantees_reattach:
        return ReattachDecision.REATTACH
    return ReattachDecision.PRESERVE_UNVERIFIABLE
