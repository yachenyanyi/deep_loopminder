"""Deterministic worker selection from current provider/runtime facts."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from src.runtime.routing.models import RoleRequirement, WorkerCandidate


class RoutingStatus(StrEnum):
    """Whether a current worker can satisfy the routing requirement."""

    SELECTED = "selected"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """Explainable provider-neutral routing result."""

    status: RoutingStatus
    provider_id: str | None
    reason: str
    eligible_provider_ids: tuple[str, ...]


def route_worker(
    requirement: RoleRequirement,
    candidates: Iterable[WorkerCandidate],
) -> RoutingDecision:
    """Select deterministically from currently available eligible workers.

    Reported capabilities are used only for routing eligibility. This function does
    not grant Tool access, authorize execution, or own provider session lifecycle.
    """
    eligible = sorted(
        (
            candidate
            for candidate in candidates
            if candidate.runtime_available
            and requirement.role in candidate.roles
            and requirement.required_capabilities <= candidate.reported_capabilities
        ),
        key=lambda candidate: candidate.provider_id,
    )
    provider_ids = tuple(candidate.provider_id for candidate in eligible)
    if not eligible:
        return RoutingDecision(
            status=RoutingStatus.BLOCKED,
            provider_id=None,
            reason="no currently available worker satisfies role and required capabilities",
            eligible_provider_ids=(),
        )

    selected = eligible[0]
    return RoutingDecision(
        status=RoutingStatus.SELECTED,
        provider_id=selected.provider_id,
        reason="selected deterministic first eligible provider from current runtime facts",
        eligible_provider_ids=provider_ids,
    )
