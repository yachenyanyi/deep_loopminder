from src.middlewares.execution.validation_policy import (
    Conformance,
    EvidenceFact,
    Independence,
    ReviewFact,
    SubjectRef,
    ValidationStatus,
    validate_evidence,
    validate_independent_review,
    validate_required_mechanism,
)


def subject(version="v1"):
    return SubjectRef("artifact:answer", version)


def test_current_subject_evidence_passes():
    result = validate_evidence(
        current_subject=subject(),
        evidence=EvidenceFact(subject(), True, "test:1"),
    )
    assert result.status is ValidationStatus.PASS


def test_old_pass_does_not_prove_mutated_subject():
    result = validate_evidence(
        current_subject=subject("v2"),
        evidence=EvidenceFact(subject("v1"), True, "test:old"),
    )
    assert result.status is ValidationStatus.FAIL
    assert result.reason == "stale_subject_evidence"


def test_validator_unavailable_is_not_subject_failure():
    result = validate_evidence(current_subject=subject(), evidence=None)
    assert result.status is ValidationStatus.UNAVAILABLE


def test_required_mechanism_is_independent_from_correctness():
    assert validate_required_mechanism(required=True, observed=False) is Conformance.NOT_EXERCISED
    assert validate_required_mechanism(required=True, observed=True) is Conformance.CONFORMANT
    assert validate_required_mechanism(required=True, observed=None) is Conformance.UNAVAILABLE
    assert validate_required_mechanism(required=False, observed=False) is None


def test_independent_review_is_optional_when_contract_does_not_require_it():
    result = validate_independent_review(current_subject=subject(), review=None, required=False)
    assert result.status is ValidationStatus.PASS
    assert result.independence is None


def test_same_execution_identity_is_not_independent_review():
    review = ReviewFact(subject(), "worker:1", "worker:1", True, "review:1")
    result = validate_independent_review(current_subject=subject(), review=review, required=True)
    assert result.status is ValidationStatus.FAIL
    assert result.independence is Independence.SAME_ACTOR


def test_role_label_cannot_replace_trusted_reviewer_identity():
    review = ReviewFact(subject(), "worker:1", None, True, "review:1")
    result = validate_independent_review(current_subject=subject(), review=review, required=True)
    assert result.status is ValidationStatus.UNAVAILABLE
    assert result.independence is Independence.UNVERIFIABLE


def test_review_of_old_subject_is_stale_after_mutation():
    review = ReviewFact(subject("v1"), "worker:1", "worker:2", True, "review:1")
    result = validate_independent_review(current_subject=subject("v2"), review=review, required=True)
    assert result.status is ValidationStatus.FAIL
    assert result.reason == "stale_review"


def test_independent_reviewer_can_reject_current_subject():
    review = ReviewFact(subject(), "worker:1", "worker:2", False, "review:1")
    result = validate_independent_review(current_subject=subject(), review=review, required=True)
    assert result.status is ValidationStatus.FAIL
    assert result.independence is Independence.INDEPENDENT


def test_current_subject_independent_approval_passes():
    review = ReviewFact(subject(), "worker:1", "worker:2", True, "review:1")
    result = validate_independent_review(current_subject=subject(), review=review, required=True)
    assert result.status is ValidationStatus.PASS
    assert result.independence is Independence.INDEPENDENT
