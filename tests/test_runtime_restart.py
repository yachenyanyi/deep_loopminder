from src.runtime.restart import (
    NativeSessionContinuityFact,
    ProcessContinuityStatus,
    ReattachDecision,
    RestartReattachFact,
    process_continuity_after_session_resume,
    restart_reattach_decision,
)


def test_persisted_process_handle_does_not_prove_reattach_after_restart() -> None:
    fact = RestartReattachFact(
        persisted_handle="pid:4242",
        lookup_supported=False,
        execution_found=True,
        provider_guarantees_reattach=True,
    )

    assert restart_reattach_decision(fact) is ReattachDecision.PRESERVE_UNVERIFIABLE


def test_fresh_provider_lookup_and_contract_allow_reattach() -> None:
    fact = RestartReattachFact(
        persisted_handle="provider:execution/42",
        lookup_supported=True,
        execution_found=True,
        provider_guarantees_reattach=True,
    )

    assert restart_reattach_decision(fact) is ReattachDecision.REATTACH


def test_missing_execution_requires_reconcile_before_new_generation() -> None:
    fact = RestartReattachFact(
        persisted_handle="pane:old",
        lookup_supported=True,
        execution_found=False,
    )

    assert (
        restart_reattach_decision(fact)
        is ReattachDecision.RECONCILE_NEW_GENERATION
    )


def test_live_resource_without_reattach_guarantee_remains_unverifiable() -> None:
    fact = RestartReattachFact(
        persisted_handle="session:42",
        lookup_supported=True,
        execution_found=True,
        provider_guarantees_reattach=False,
    )

    assert restart_reattach_decision(fact) is ReattachDecision.PRESERVE_UNVERIFIABLE


def test_native_session_resume_does_not_prove_process_continuity() -> None:
    fact = NativeSessionContinuityFact(
        native_session_resumed=True,
        process_lookup_supported=False,
        process_found=True,
        provider_guarantees_same_execution=True,
    )

    assert (
        process_continuity_after_session_resume(fact)
        is ProcessContinuityStatus.UNVERIFIABLE
    )


def test_native_session_can_resume_after_original_process_is_gone() -> None:
    fact = NativeSessionContinuityFact(
        native_session_resumed=True,
        process_lookup_supported=True,
        process_found=False,
    )

    assert (
        process_continuity_after_session_resume(fact)
        is ProcessContinuityStatus.NOT_PROVEN
    )


def test_found_process_without_same_execution_guarantee_is_unverifiable() -> None:
    fact = NativeSessionContinuityFact(
        native_session_resumed=True,
        process_lookup_supported=True,
        process_found=True,
        provider_guarantees_same_execution=False,
    )

    assert (
        process_continuity_after_session_resume(fact)
        is ProcessContinuityStatus.UNVERIFIABLE
    )


def test_provider_process_lookup_and_same_execution_contract_prove_continuity() -> None:
    fact = NativeSessionContinuityFact(
        native_session_resumed=True,
        process_lookup_supported=True,
        process_found=True,
        provider_guarantees_same_execution=True,
    )

    assert (
        process_continuity_after_session_resume(fact)
        is ProcessContinuityStatus.PROVEN
    )
