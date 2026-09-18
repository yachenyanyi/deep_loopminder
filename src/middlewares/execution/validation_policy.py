"""Deterministic validation policy primitives for Issue #31.

Semantic grading, structured-output parsing, task lifecycle, persistence, and
review execution remain owned by official LangChain/LangGraph/Deep Agents
mechanisms.  This module only evaluates trusted facts already produced by
those runtimes/providers.
"""

from dataclasses import dataclass
from enum import Enum


class ValidationStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNAVAILABLE = "unavailable"


class Conformance(str, Enum):
    CONFORMANT = "conformant"
    NOT_EXERCISED = "not_exercised"
    UNAVAILABLE = "unavailable"


class Independence(str, Enum):
    INDEPENDENT = "independent"
    SAME_ACTOR = "same_actor"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True, slots=True)
class SubjectRef:
    """Immutable identity of exactly what an evidence item validates."""

    identity: str
    version: str

    def __post_init__(self) -> None:
        if not self.identity.strip() or not self.version.strip():
            raise ValueError("subject identity and version must be non-empty")


@dataclass(frozen=True, slots=True)
class EvidenceFact:
    subject: SubjectRef
    passed: bool
    evidence_ref: str

    def __post_init__(self) -> None:
        if not self.evidence_ref.strip():
            raise ValueError("evidence_ref must be non-empty")


@dataclass(frozen=True, slots=True)
class ReviewFact:
    subject: SubjectRef
    implementer_identity: str | None
    reviewer_identity: str | None
    approved: bool
    evidence_ref: str

    def __post_init__(self) -> None:
        if not self.evidence_ref.strip():
            raise ValueError("evidence_ref must be non-empty")


@dataclass(frozen=True, slots=True)
class ValidationResult:
    status: ValidationStatus
    reason: str
    conformance: Conformance | None = None
    independence: Independence | None = None


def validate_evidence(*, current_subject: SubjectRef, evidence: EvidenceFact | None) -> ValidationResult:
    if evidence is None:
        return ValidationResult(ValidationStatus.UNAVAILABLE, "evidence_unavailable")
    if evidence.subject != current_subject:
        return ValidationResult(ValidationStatus.FAIL, "stale_subject_evidence")
    if not evidence.passed:
        return ValidationResult(ValidationStatus.FAIL, "subject_failed")
    return ValidationResult(ValidationStatus.PASS, "current_subject_passed")


def validate_required_mechanism(*, required: bool, observed: bool | None) -> Conformance | None:
    """Validate a mechanism fact without scheduling or inferring execution."""

    if not required:
        return None
    if observed is None:
        return Conformance.UNAVAILABLE
    return Conformance.CONFORMANT if observed else Conformance.NOT_EXERCISED


def validate_independent_review(
    *,
    current_subject: SubjectRef,
    review: ReviewFact | None,
    required: bool,
) -> ValidationResult:
    """Check an explicit independent-review contract from trusted identities."""

    if not required:
        return ValidationResult(ValidationStatus.PASS, "independent_review_not_required")
    if review is None:
        return ValidationResult(
            ValidationStatus.UNAVAILABLE,
            "review_unavailable",
            independence=Independence.UNVERIFIABLE,
        )
    if review.subject != current_subject:
        return ValidationResult(
            ValidationStatus.FAIL,
            "stale_review",
            independence=Independence.UNVERIFIABLE,
        )
    if not review.implementer_identity or not review.reviewer_identity:
        return ValidationResult(
            ValidationStatus.UNAVAILABLE,
            "reviewer_identity_unverifiable",
            independence=Independence.UNVERIFIABLE,
        )
    if review.implementer_identity == review.reviewer_identity:
        return ValidationResult(
            ValidationStatus.FAIL,
            "reviewer_not_independent",
            independence=Independence.SAME_ACTOR,
        )
    if not review.approved:
        return ValidationResult(
            ValidationStatus.FAIL,
            "independent_review_rejected",
            independence=Independence.INDEPENDENT,
        )
    return ValidationResult(
        ValidationStatus.PASS,
        "independent_review_passed",
        independence=Independence.INDEPENDENT,
    )
