from src.runtime.teardown import (
    OperationClearance,
    TeardownDecision,
    TeardownFact,
    teardown_decision,
)


def test_destroy_requires_every_destructive_precondition() -> None:
    decision = teardown_decision(
        TeardownFact(
            current_owner=True,
            current_generation=True,
            operation_clearance=OperationClearance.CLEAR,
            required_outputs_durable=True,
            provider_safe_destroy=True,
        )
    )

    assert decision is TeardownDecision.DESTROY_ALLOWED


def test_unknown_owner_preserves_resource_for_diagnostics() -> None:
    decision = teardown_decision(
        TeardownFact(
            current_owner=None,
            current_generation=True,
            operation_clearance=OperationClearance.CLEAR,
            required_outputs_durable=True,
            provider_safe_destroy=True,
        )
    )

    assert decision is TeardownDecision.PRESERVE_DIAGNOSTIC


def test_stale_generation_cannot_destroy_current_resource() -> None:
    decision = teardown_decision(
        TeardownFact(
            current_owner=True,
            current_generation=False,
            operation_clearance=OperationClearance.CLEAR,
            required_outputs_durable=True,
            provider_safe_destroy=True,
        )
    )

    assert decision is TeardownDecision.PRESERVE_DIAGNOSTIC


def test_in_flight_operation_blocks_destroy() -> None:
    decision = teardown_decision(
        TeardownFact(
            current_owner=True,
            current_generation=True,
            operation_clearance=OperationClearance.IN_FLIGHT,
            required_outputs_durable=True,
            provider_safe_destroy=True,
        )
    )

    assert decision is TeardownDecision.PRESERVE_DIAGNOSTIC


def test_unknown_operation_outcome_blocks_destroy() -> None:
    decision = teardown_decision(
        TeardownFact(
            current_owner=True,
            current_generation=True,
            operation_clearance=OperationClearance.UNKNOWN,
            required_outputs_durable=True,
            provider_safe_destroy=True,
        )
    )

    assert decision is TeardownDecision.PRESERVE_DIAGNOSTIC


def test_non_durable_required_outputs_surface_artifact_loss_risk() -> None:
    decision = teardown_decision(
        TeardownFact(
            current_owner=True,
            current_generation=True,
            operation_clearance=OperationClearance.CLEAR,
            required_outputs_durable=False,
            provider_safe_destroy=True,
        )
    )

    assert decision is TeardownDecision.PRESERVE_ARTIFACT_LOSS_RISK


def test_unknown_output_durability_blocks_destroy() -> None:
    decision = teardown_decision(
        TeardownFact(
            current_owner=True,
            current_generation=True,
            operation_clearance=OperationClearance.CLEAR,
            required_outputs_durable=None,
            provider_safe_destroy=True,
        )
    )

    assert decision is TeardownDecision.PRESERVE_DIAGNOSTIC


def test_provider_without_safe_destroy_proof_is_preserved() -> None:
    decision = teardown_decision(
        TeardownFact(
            current_owner=True,
            current_generation=True,
            operation_clearance=OperationClearance.CLEAR,
            required_outputs_durable=True,
            provider_safe_destroy=None,
        )
    )

    assert decision is TeardownDecision.PRESERVE_DIAGNOSTIC
