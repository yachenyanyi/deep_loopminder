from src.runtime.reconciliation import (
    ExecutionResumeFact,
    ProviderOperationFact,
    ReconcileAction,
    ResumeCapability,
    execution_resume_capability,
    reconcile_operation,
)


def test_completed_receipt_is_reused_instead_of_replayed() -> None:
    fact = ProviderOperationFact(
        operation_id="deploy:42",
        lookup_supported=True,
        outcome="completed",
        receipt_ref="provider:job/42",
    )

    assert reconcile_operation(fact) is ReconcileAction.REUSE_COMPLETED


def test_unknown_mutation_is_not_blindly_retried() -> None:
    fact = ProviderOperationFact(
        operation_id="deploy:42",
        lookup_supported=True,
        outcome="unknown",
        retry_safe=True,
    )

    assert reconcile_operation(fact) is ReconcileAction.PRESERVE_UNKNOWN


def test_missing_lookup_capability_is_conservative() -> None:
    fact = ProviderOperationFact(
        operation_id="deploy:42",
        lookup_supported=False,
        retry_safe=True,
    )

    assert reconcile_operation(fact) is ReconcileAction.PRESERVE_UNKNOWN


def test_proven_failed_operation_can_retry_only_when_provider_says_safe() -> None:
    safe = ProviderOperationFact(
        operation_id="read:42",
        lookup_supported=True,
        outcome="failed",
        retry_safe=True,
    )
    unsafe = ProviderOperationFact(
        operation_id="write:42",
        lookup_supported=True,
        outcome="failed",
        retry_safe=False,
    )

    assert reconcile_operation(safe) is ReconcileAction.RETRY_SAFE
    assert reconcile_operation(unsafe) is ReconcileAction.PRESERVE_UNKNOWN


def test_live_execution_requires_provider_resume_guarantee() -> None:
    fact = ExecutionResumeFact(
        lookup_supported=True,
        execution_found=True,
        provider_guarantees_resume=False,
    )

    assert execution_resume_capability(fact) is ResumeCapability.UNVERIFIABLE


def test_provider_proof_can_mark_execution_resumable() -> None:
    fact = ExecutionResumeFact(
        lookup_supported=True,
        execution_found=True,
        provider_guarantees_resume=True,
    )

    assert execution_resume_capability(fact) is ResumeCapability.RESUMABLE


def test_missing_execution_is_not_resumable() -> None:
    fact = ExecutionResumeFact(
        lookup_supported=True,
        execution_found=False,
        provider_guarantees_resume=True,
    )

    assert execution_resume_capability(fact) is ResumeCapability.NOT_RESUMABLE


def test_unavailable_lookup_is_execution_resume_unverifiable() -> None:
    fact = ExecutionResumeFact(
        lookup_supported=False,
        execution_found=None,
        provider_guarantees_resume=True,
    )

    assert execution_resume_capability(fact) is ResumeCapability.UNVERIFIABLE
