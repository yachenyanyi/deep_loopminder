from src.runtime.restart import (
    ReattachDecision,
    RestartReattachFact,
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
