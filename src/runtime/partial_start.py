"""Project partial remote-start failures without owning provider lifecycle.

This module records the minimum facts needed to reconcile a compound start that
failed after one or more provider-side resources may already exist. It does not
create, retry, destroy, or persist provider resources itself.
"""

from dataclasses import dataclass
from enum import StrEnum


class StartStageOutcome(StrEnum):
    """Provider-visible outcome for the stage that stopped compound startup."""

    FAILED = "failed"
    UNKNOWN = "unknown"


class ResidualReconcileAction(StrEnum):
    """Conservative next action after a compound-start stage failure."""

    RECONCILE_RESIDUALS = "reconcile_residuals"
    RECONCILE_OUTCOME = "reconcile_outcome"
    RETRY_FAILED_STAGE = "retry_failed_stage"


@dataclass(frozen=True, slots=True)
class PartialStartFact:
    """Facts supplied by the owning provider/runtime adapter after start failure."""

    operation_id: str
    failed_stage: str
    outcome: StartStageOutcome
    residual_refs: tuple[str, ...] = ()
    provider_refs: tuple[str, ...] = ()
    provider_proves_no_residuals: bool = False
    provider_guarantees_stage_retry: bool = False


@dataclass(frozen=True, slots=True)
class PartialStartProjection:
    """Minimal durable projection used to drive later provider reconciliation."""

    operation_id: str
    failed_stage: str
    outcome: StartStageOutcome
    residual_refs: tuple[str, ...]
    provider_refs: tuple[str, ...]
    recommended_action: ResidualReconcileAction


def project_partial_start(fact: PartialStartFact) -> PartialStartProjection:
    """Choose reconciliation without turning a stage failure into full relaunch.

    Known residual resources are reconciled first. Unknown stage outcome also
    requires provider reconciliation. Retrying only the failed stage is allowed
    when the provider proves there are no residual resources and explicitly
    guarantees that stage retry is safe.
    """
    if fact.residual_refs:
        action = ResidualReconcileAction.RECONCILE_RESIDUALS
    elif fact.outcome is StartStageOutcome.UNKNOWN:
        action = ResidualReconcileAction.RECONCILE_OUTCOME
    elif fact.provider_proves_no_residuals and fact.provider_guarantees_stage_retry:
        action = ResidualReconcileAction.RETRY_FAILED_STAGE
    else:
        action = ResidualReconcileAction.RECONCILE_OUTCOME

    return PartialStartProjection(
        operation_id=fact.operation_id,
        failed_stage=fact.failed_stage,
        outcome=fact.outcome,
        residual_refs=fact.residual_refs,
        provider_refs=fact.provider_refs,
        recommended_action=action,
    )
