from src.runtime.reconciliation import (
    CancellationFact,
    CancellationSafety,
    ExecutionResumeFact,
    FencingStrength,
    GenerationDecision,
    GenerationFact,
    LivenessStatus,
    ProviderOperationFact,
    ReconcileAction,
    ResumeCapability,
    RuntimeLivenessFact,
    cancellation_safety,
    execution_resume_capability,
    generation_authority,
    reconcile_operation,
    runtime_liveness,
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


def test_fresh_provider_lookup_can_prove_runtime_is_live() -> None:
    fact = RuntimeLivenessFact(lookup_supported=True, resource_found=True)

    assert runtime_liveness(fact) is LivenessStatus.LIVE


def test_fresh_provider_lookup_can_prove_runtime_is_missing() -> None:
    fact = RuntimeLivenessFact(lookup_supported=True, resource_found=False)

    assert runtime_liveness(fact) is LivenessStatus.NOT_FOUND


def test_persisted_identity_without_lookup_is_not_liveness_proof() -> None:
    unsupported = RuntimeLivenessFact(lookup_supported=False, resource_found=True)
    ambiguous = RuntimeLivenessFact(lookup_supported=True, resource_found=None)

    assert runtime_liveness(unsupported) is LivenessStatus.UNVERIFIABLE
    assert runtime_liveness(ambiguous) is LivenessStatus.UNVERIFIABLE


def test_cancel_delivery_and_stream_stop_do_not_prove_commit_safety() -> None:
    fact = CancellationFact(
        requested=True,
        delivered=True,
        stream_stopped=True,
        execution_stopped=None,
        commit_safe=None,
    )

    assert cancellation_safety(fact) is CancellationSafety.UNVERIFIABLE


def test_execution_stop_alone_does_not_prove_external_side_effect_safety() -> None:
    fact = CancellationFact(
        requested=True,
        delivered=True,
        stream_stopped=True,
        execution_stopped=True,
        commit_safe=None,
    )

    assert cancellation_safety(fact) is CancellationSafety.UNVERIFIABLE


def test_provider_can_explicitly_deny_commit_safety_after_cancel() -> None:
    fact = CancellationFact(
        requested=True,
        delivered=True,
        execution_stopped=False,
        commit_safe=False,
    )

    assert cancellation_safety(fact) is CancellationSafety.NOT_COMMIT_SAFE


def test_provider_proof_is_required_for_commit_safe_cancel() -> None:
    fact = CancellationFact(
        requested=True,
        delivered=True,
        stream_stopped=True,
        execution_stopped=True,
        commit_safe=True,
    )

    assert cancellation_safety(fact) is CancellationSafety.COMMIT_SAFE


def test_late_result_from_old_generation_is_rejected() -> None:
    authority = generation_authority(
        GenerationFact(
            current_generation="runtime:2",
            result_generation="runtime:1",
        )
    )

    assert authority.decision is GenerationDecision.REJECT_STALE
    assert authority.fencing is FencingStrength.BEST_EFFORT


def test_current_generation_result_is_accepted() -> None:
    authority = generation_authority(
        GenerationFact(
            current_generation="runtime:2",
            result_generation="runtime:2",
        )
    )

    assert authority.decision is GenerationDecision.ACCEPT_CURRENT
    assert authority.fencing is FencingStrength.BEST_EFFORT


def test_provider_fencing_guarantee_is_reported_as_strong() -> None:
    authority = generation_authority(
        GenerationFact(
            current_generation="lease:9",
            result_generation="lease:8",
            provider_fencing_guaranteed=True,
        )
    )

    assert authority.decision is GenerationDecision.REJECT_STALE
    assert authority.fencing is FencingStrength.STRONG
