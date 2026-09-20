"""Conservative remote-runtime teardown authorization.

The policy consumes current ownership, operation, output-durability, and
provider destroy-safety facts. It does not discover, stop, destroy, or persist
provider resources itself.
"""

from dataclasses import dataclass
from enum import StrEnum


class OperationClearance(StrEnum):
    """Current provider-backed clearance for external operations."""

    CLEAR = "clear"
    IN_FLIGHT = "in_flight"
    UNKNOWN = "unknown"


class TeardownDecision(StrEnum):
    """Conservative authorization result for remote resource teardown."""

    DESTROY_ALLOWED = "destroy_allowed"
    PRESERVE_DIAGNOSTIC = "preserve_diagnostic"
    PRESERVE_ARTIFACT_LOSS_RISK = "preserve_artifact_loss_risk"


@dataclass(frozen=True, slots=True)
class TeardownFact:
    """Fresh facts required before destructive remote cleanup."""

    current_owner: bool | None
    current_generation: bool | None
    operation_clearance: OperationClearance
    required_outputs_durable: bool | None
    provider_safe_destroy: bool | None


def teardown_decision(fact: TeardownFact) -> TeardownDecision:
    """Authorize destroy only when every destructive precondition is proven.

    Required outputs known to be non-durable are surfaced as an artifact-loss
    risk. Every other missing, stale, conflicting, or in-flight fact preserves
    the resource for reconciliation and diagnostics.
    """
    if fact.required_outputs_durable is False:
        return TeardownDecision.PRESERVE_ARTIFACT_LOSS_RISK

    if (
        fact.current_owner is not True
        or fact.current_generation is not True
        or fact.operation_clearance is not OperationClearance.CLEAR
        or fact.required_outputs_durable is not True
        or fact.provider_safe_destroy is not True
    ):
        return TeardownDecision.PRESERVE_DIAGNOSTIC

    return TeardownDecision.DESTROY_ALLOWED
