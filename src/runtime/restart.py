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


class ProcessContinuityStatus(StrEnum):
    """Whether the original provider execution continuity is actually proven."""

    PROVEN = "proven"
    NOT_PROVEN = "not_proven"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class RestartReattachFact:
    """Fresh provider evidence for an execution referenced before host restart.

    persisted_handle is diagnostic identity only. A PID, pane id, process
    handle, native session id, or conversation id cannot establish liveness.
    execution_found must come from a fresh provider/runtime lookup.
    provider_guarantees_reattach must reflect the provider documented
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


@dataclass(frozen=True, slots=True)
class NativeSessionContinuityFact:
    """Provider-native session context plus fresh process-continuity evidence.

    native_session_resumed is intentionally not authoritative for the original
    shell/process. Only fresh process lookup plus a provider guarantee that the
    located execution is the same original execution can prove continuity.
    """

    native_session_resumed: bool
    process_lookup_supported: bool
    process_found: bool | None = None
    provider_guarantees_same_execution: bool = False


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


def process_continuity_after_session_resume(
    fact: NativeSessionContinuityFact,
) -> ProcessContinuityStatus:
    """Keep provider-native session resume separate from process continuity.

    Resuming a provider conversation/session is context only. A process lookup
    that cannot establish existence remains unverifiable; an explicit not-found
    result disproves continuity; and a found process is only proven continuous
    when the provider documents that it is the same original execution.
    """
    if not fact.process_lookup_supported or fact.process_found is None:
        return ProcessContinuityStatus.UNVERIFIABLE
    if not fact.process_found:
        return ProcessContinuityStatus.NOT_PROVEN
    if fact.provider_guarantees_same_execution:
        return ProcessContinuityStatus.PROVEN
    return ProcessContinuityStatus.UNVERIFIABLE
