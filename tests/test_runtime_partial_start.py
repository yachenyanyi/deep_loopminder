from src.runtime.partial_start import (
    PartialStartFact,
    ResidualReconcileAction,
    StartStageOutcome,
    project_partial_start,
)


def test_known_residual_resources_are_reconciled_before_retry() -> None:
    projection = project_partial_start(
        PartialStartFact(
            operation_id="start:42",
            failed_stage="launch-process",
            outcome=StartStageOutcome.FAILED,
            residual_refs=("sandbox:abc", "workspace:def"),
            provider_refs=("job:123",),
            provider_proves_no_residuals=False,
            provider_guarantees_stage_retry=True,
        )
    )

    assert projection.residual_refs == ("sandbox:abc", "workspace:def")
    assert projection.provider_refs == ("job:123",)
    assert (
        projection.recommended_action
        is ResidualReconcileAction.RECONCILE_RESIDUALS
    )


def test_unknown_stage_outcome_requires_reconcile_even_when_retry_is_supported() -> None:
    projection = project_partial_start(
        PartialStartFact(
            operation_id="start:43",
            failed_stage="attach-session",
            outcome=StartStageOutcome.UNKNOWN,
            provider_proves_no_residuals=True,
            provider_guarantees_stage_retry=True,
        )
    )

    assert (
        projection.recommended_action is ResidualReconcileAction.RECONCILE_OUTCOME
    )


def test_failed_stage_retry_requires_explicit_no_residual_and_retry_guarantees() -> None:
    projection = project_partial_start(
        PartialStartFact(
            operation_id="start:44",
            failed_stage="launch-process",
            outcome=StartStageOutcome.FAILED,
            provider_proves_no_residuals=True,
            provider_guarantees_stage_retry=True,
        )
    )

    assert projection.recommended_action is ResidualReconcileAction.RETRY_FAILED_STAGE


def test_failed_stage_without_provider_proof_does_not_imply_full_relaunch() -> None:
    projection = project_partial_start(
        PartialStartFact(
            operation_id="start:45",
            failed_stage="launch-process",
            outcome=StartStageOutcome.FAILED,
        )
    )

    assert (
        projection.recommended_action is ResidualReconcileAction.RECONCILE_OUTCOME
    )
