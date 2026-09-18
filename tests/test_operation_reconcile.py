import pytest

from src.middlewares.execution.operation_reconcile import (
    OperationOutcome,
    OperationState,
    ReconcileAction,
    reconcile_action,
)


def test_crash_before_external_call_allows_execution():
    state = OperationState(operation_id="op-a")

    assert reconcile_action(
        state,
        provider_lookup_available=False,
        provider_idempotency_available=False,
    ) is ReconcileAction.EXECUTE


def test_unknown_outcome_prefers_provider_lookup_over_retry():
    state = OperationState(
        operation_id="op-b",
        outcome=OperationOutcome.OUTCOME_UNKNOWN,
        provider_operation_ref="provider-job-1",
    )

    assert reconcile_action(
        state,
        provider_lookup_available=True,
        provider_idempotency_available=True,
    ) is ReconcileAction.LOOKUP


def test_unknown_outcome_can_retry_only_with_provider_idempotency():
    state = OperationState(operation_id="op-c", outcome=OperationOutcome.OUTCOME_UNKNOWN)

    assert reconcile_action(
        state,
        provider_lookup_available=False,
        provider_idempotency_available=True,
    ) is ReconcileAction.EXECUTE


def test_unknown_outcome_without_reconcile_primitive_fails_closed():
    state = OperationState(operation_id="op-d", outcome=OperationOutcome.OUTCOME_UNKNOWN)

    assert reconcile_action(
        state,
        provider_lookup_available=False,
        provider_idempotency_available=False,
    ) is ReconcileAction.STOP


def test_durable_completion_is_replayed_not_executed_again():
    state = OperationState(
        operation_id="op-e",
        outcome=OperationOutcome.COMPLETED,
        completion_ref="artifact://completion/op-e",
    )

    assert reconcile_action(
        state,
        provider_lookup_available=True,
        provider_idempotency_available=True,
    ) is ReconcileAction.REPLAY_COMPLETION


def test_completed_operation_requires_durable_completion_reference():
    with pytest.raises(ValueError, match="completion_ref"):
        OperationState(operation_id="op-f", outcome=OperationOutcome.COMPLETED)


def test_completion_reference_cannot_claim_non_completed_outcome():
    with pytest.raises(ValueError, match="completion_ref"):
        OperationState(operation_id="op-g", completion_ref="artifact://unexpected")
