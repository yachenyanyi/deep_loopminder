"""Deterministic provider reconciliation policy for remote execution.

The types in this module deliberately consume provider/runtime facts rather
than mirroring LangGraph run, thread, checkpoint, or stream state. They help
callers decide whether an external operation may be retried, whether a
persisted execution reference is safe to resume, and whether cancellation has
actually made replacement/commit safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReconcileAction(str, Enum):
    """Conservative next action after looking up a provider operation."""

    REUSE_COMPLETED = "reuse_completed"
    RETRY_SAFE = "retry_safe"
    WAIT_OR_RECONCILE = "wait_or_reconcile"
    PRESERVE_UNKNOWN = "preserve_unknown"


class ResumeCapability(str, Enum):
    """Provider execution resumability, independent from graph resumability."""

    RESUMABLE = "resumable"
    NOT_RESUMABLE = "not_resumable"
    UNVERIFIABLE = "unverifiable"


class CancellationSafety(str, Enum):
    """Authority decision derived from provider cancellation facts."""

    COMMIT_SAFE = "commit_safe"
    NOT_COMMIT_SAFE = "not_commit_safe"
    UNVERIFIABLE = "unverifiable"


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
