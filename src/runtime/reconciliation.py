"""Deterministic provider reconciliation policy for remote execution.

The types in this module deliberately consume provider/runtime facts rather
than mirroring LangGraph run, thread, checkpoint, or stream state. They help
callers decide whether an external operation may be retried, whether a
persisted execution reference is safe to resume, whether cancellation has
actually made replacement/commit safe, and whether a late result still belongs
to the current runtime generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReconcileAction(StrEnum):
    """Conservative next action after looking up a provider operation."""

    REUSE_COMPLETED = "reuse_completed"
    RETRY_SAFE = "retry_safe"
    WAIT_OR_RECONCILE = "wait_or_reconcile"
    PRESERVE_UNKNOWN = "preserve_unknown"


class ResumeCapability(StrEnum):
    """Provider execution resumability, independent from graph resumability."""

    RESUMABLE = "resumable"
    NOT_RESUMABLE = "not_resumable"
    UNVERIFIABLE = "unverifiable"


class LivenessStatus(StrEnum):
    """Use-time provider liveness result for a persisted runtime reference."""

    LIVE = "live"
    NOT_FOUND = "not_found"
    UNVERIFIABLE = "unverifiable"


class TransportOutcome(StrEnum):
    """What a transport observation can prove about remote execution."""

    RESPONSE_RECEIVED = "response_received"
    CONTACT_LOST = "contact_lost"
    EMPTY_WAIT = "empty_wait"


class CancellationSafety(StrEnum):
    """Authority decision derived from provider cancellation facts."""

    COMMIT_SAFE = "commit_safe"
    NOT_COMMIT_SAFE = "not_commit_safe"
    UNVERIFIABLE = "unverifiable"


class GenerationDecision(StrEnum):
    """Whether a result may commit against the current runtime generation."""

    ACCEPT_CURRENT = "accept_current"
    REJECT_STALE = "reject_stale"


class FencingStrength(StrEnum):
    """Strength of the fencing evidence backing a generation decision."""

    STRONG = "strong"
    BEST_EFFORT = "best_effort"


@dataclass(frozen=True, slots=True)
class ProviderOperationFact:
    """Machine-readable fact returned by the provider/runtime lookup path.

    ``outcome`` is intentionally small: the provider adapter must normalize
    only outcomes it can prove. Missing lookup support is represented by
    ``lookup_supported=False`` rather than by guessing that the operation did
    not happen.
    """

    operation_id: str
    lookup_supported: bool
    outcome: str | None = None  # completed | failed | running | unknown
    retry_safe: bool = False
    receipt_ref: str | None = None

    def __post_init__(self) -> None:
        """Validate the provider operation identity and normalized outcome."""
        if not self.operation_id.strip():
            raise ValueError("operation_id must be non-empty")
        if self.outcome not in {None, "completed", "failed", "running", "unknown"}:
            raise ValueError(f"unsupported provider outcome: {self.outcome!r}")


@dataclass(frozen=True, slots=True)
class ExecutionResumeFact:
    """Provider-native execution resume evidence.

    A LangGraph checkpoint is deliberately absent: graph resumability is owned
    by LangGraph and cannot prove that a provider process/session is live or
    replay-safe.
    """

    lookup_supported: bool
    execution_found: bool | None = None
    provider_guarantees_resume: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeLivenessFact:
    """Fresh provider lookup result for an already-persisted runtime identity.

    Persisted session, sandbox, workspace, or process references are inputs to
    the provider lookup path, not liveness evidence themselves. ``None`` means
    the lookup could not establish existence and must remain unverifiable.
    """

    lookup_supported: bool
    resource_found: bool | None = None


@dataclass(frozen=True, slots=True)
class TransportFact:
    """Transport-only observation that carries no execution outcome authority.

    A timeout, disconnect, or empty wait says only that the caller lacks a
    response. It cannot prove whether an external operation happened or whether
    the remote process is still alive. Those facts require provider-native
    receipt/status/liveness lookup.
    """

    outcome: TransportOutcome


@dataclass(frozen=True, slots=True)
class TransportAuthority:
    """Conservative authority derived from a transport observation."""

    operation_outcome_known: bool
    execution_liveness_known: bool


@dataclass(frozen=True, slots=True)
class CancellationFact:
    """Provider cancellation observations without inventing a run lifecycle.

    Each field is independent. ``None`` means the provider cannot prove that
    fact. In particular, request delivery or a stopped stream must never be
    promoted into proof that the underlying execution stopped or lost commit
    authority.
    """

    requested: bool
    delivered: bool | None = None
    stream_stopped: bool | None = None
    execution_stopped: bool | None = None
    commit_safe: bool | None = None


@dataclass(frozen=True, slots=True)
class GenerationFact:
    """Opaque generation facts supplied by the owning runtime/provider adapter.

    The generation tokens are not a new lifecycle store. Callers supply the
    current owner token and the token attached to a returned result. A provider
    lease/generation/CAS guarantee upgrades the decision to strong fencing;
    otherwise rejection is deliberately labelled best-effort.
    """

    current_generation: str
    result_generation: str
    provider_fencing_guaranteed: bool = False

    def __post_init__(self) -> None:
        """Require non-empty opaque generation identities."""
        if not self.current_generation.strip():
            raise ValueError("current_generation must be non-empty")
        if not self.result_generation.strip():
            raise ValueError("result_generation must be non-empty")


@dataclass(frozen=True, slots=True)
class GenerationAuthority:
    """Commit decision plus an explicit statement of fencing strength."""

    decision: GenerationDecision
    fencing: FencingStrength


def reconcile_operation(fact: ProviderOperationFact) -> ReconcileAction:
    """Choose the safest action without blindly replaying external mutation."""
    if not fact.lookup_supported:
        return ReconcileAction.PRESERVE_UNKNOWN
    if fact.outcome == "completed":
        return ReconcileAction.REUSE_COMPLETED
    if fact.outcome == "running":
        return ReconcileAction.WAIT_OR_RECONCILE
    if fact.outcome == "failed" and fact.retry_safe:
        return ReconcileAction.RETRY_SAFE
    return ReconcileAction.PRESERVE_UNKNOWN


def execution_resume_capability(fact: ExecutionResumeFact) -> ResumeCapability:
    """Evaluate provider execution resume capability from provider facts only."""
    if not fact.lookup_supported or fact.execution_found is None:
        return ResumeCapability.UNVERIFIABLE
    if not fact.execution_found:
        return ResumeCapability.NOT_RESUMABLE
    if fact.provider_guarantees_resume:
        return ResumeCapability.RESUMABLE
    return ResumeCapability.UNVERIFIABLE


def runtime_liveness(fact: RuntimeLivenessFact) -> LivenessStatus:
    """Classify liveness only from a fresh provider/runtime lookup result."""
    if not fact.lookup_supported or fact.resource_found is None:
        return LivenessStatus.UNVERIFIABLE
    if fact.resource_found:
        return LivenessStatus.LIVE
    return LivenessStatus.NOT_FOUND


def transport_authority(fact: TransportFact) -> TransportAuthority:
    """Keep transport contact separate from operation and liveness truth.

    A received response only establishes that transport completed; its payload
    must still be interpreted by the provider adapter. Loss of contact or an
    empty wait therefore never authorizes retry, relaunch, cleanup, or a claim
    that the remote execution exited.
    """
    del fact
    return TransportAuthority(
        operation_outcome_known=False,
        execution_liveness_known=False,
    )


def cancellation_safety(fact: CancellationFact) -> CancellationSafety:
    """Decide whether a cancelled execution has provably lost commit authority.

    ``commit_safe`` is intentionally provider/runtime evidence. We do not infer
    it from request delivery, stream termination, or even execution termination
    because external side effects may still require operation reconciliation.
    """
    if fact.commit_safe is True:
        return CancellationSafety.COMMIT_SAFE
    if fact.commit_safe is False:
        return CancellationSafety.NOT_COMMIT_SAFE
    return CancellationSafety.UNVERIFIABLE


def generation_authority(fact: GenerationFact) -> GenerationAuthority:
    """Reject late results from a superseded generation without inventing fencing.

    Equality is enough to identify a result as belonging to the caller's
    current generation. Inequality rejects a stale result locally. The fencing
    label remains best-effort unless the provider/owner exposes an actual
    lease, generation, CAS, or conditional-write guarantee; a local token alone
    must not be advertised as strong fencing.
    """
    fencing = (
        FencingStrength.STRONG
        if fact.provider_fencing_guaranteed
        else FencingStrength.BEST_EFFORT
    )
    decision = (
        GenerationDecision.ACCEPT_CURRENT
        if fact.result_generation == fact.current_generation
        else GenerationDecision.REJECT_STALE
    )
    return GenerationAuthority(decision=decision, fencing=fencing)
