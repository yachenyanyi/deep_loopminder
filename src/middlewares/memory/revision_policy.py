"""Provider-neutral memory revision policy for Issue #26.

This module deliberately does not implement storage, retrieval, indexing,
consolidation, tracing, or scheduling. Those remain responsibilities of the
official LangGraph Store / Deep Agents Memory and adjacent project concerns.
"""

from dataclasses import dataclass
from enum import Enum


class MemoryRevisionState(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RECALL_DELETED = "recall_deleted"


@dataclass(frozen=True, slots=True)
class MemoryRevision:
    """Stable identity and current-recall policy for one memory revision."""

    memory_ref: str
    version: str
    content_hash: str
    state: MemoryRevisionState = MemoryRevisionState.ACTIVE
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        if not self.memory_ref:
            raise ValueError("memory_ref must be non-empty")
        if not self.version:
            raise ValueError("version must be non-empty")
        if not self.content_hash:
            raise ValueError("content_hash must be non-empty")

        if self.state is MemoryRevisionState.SUPERSEDED:
            if not self.superseded_by:
                raise ValueError("superseded revision requires superseded_by")
            if self.superseded_by == self.memory_ref:
                raise ValueError("revision cannot supersede itself")
        elif self.superseded_by is not None:
            raise ValueError("superseded_by is only valid for superseded revisions")

    @property
    def selectable_for_current_recall(self) -> bool:
        """Whether this revision may appear in the default current memory view."""

        return self.state is MemoryRevisionState.ACTIVE


@dataclass(frozen=True, slots=True)
class MemoryInjectionEvidence:
    """Immutable identity of the revision/projection a model call actually saw.

    This intentionally does not resolve to a successor revision. Historical
    evidence must keep the inject-time version/hash even after future
    supersede or recall deletion.
    """

    memory_ref: str
    version: str
    content_hash: str
    projection_ref: str | None = None

    @classmethod
    def from_revision(
        cls,
        revision: MemoryRevision,
        *,
        projection_ref: str | None = None,
    ) -> "MemoryInjectionEvidence":
        return cls(
            memory_ref=revision.memory_ref,
            version=revision.version,
            content_hash=revision.content_hash,
            projection_ref=projection_ref,
        )


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Control-plane decision for destructive deletion.

    Recall deletion is represented by MemoryRevisionState.RECALL_DELETED and
    is independent from physical deletion. Hard deletion requires an explicit
    retention/privacy decision supplied by trusted control-plane code.
    """

    allow_hard_delete: bool = False


def can_hard_delete(*, policy: RetentionPolicy) -> bool:
    return policy.allow_hard_delete
