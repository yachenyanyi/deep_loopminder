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
    rejected_reasons: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _rejection_reasons(
    requirement: RoleRequirement,
    candidate: WorkerCandidate,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not candidate.runtime_available:
        reasons.append("runtime unavailable")
    if requirement.role not in candidate.roles:
        reasons.append(f"role {requirement.role!r} not supported")
    missing = sorted(requirement.required_capabilities - candidate.reported_capabilities)
    if missing:
        reasons.append(f"missing required capabilities: {', '.join(missing)}")
    mismatched = sorted(
        key
        for key, expected in requirement.constraints.items()
        if candidate.compatibility.get(key) != expected
    )
    if mismatched:
        reasons.append(f"constraint mismatch: {', '.join(mismatched)}")
    return tuple(reasons)


def route_worker(
    requirement: RoleRequirement,
    candidates: Iterable[WorkerCandidate],
) -> RoutingDecision:
    """Select deterministically from current trusted eligibility facts.

    Reported capabilities and compatibility facts are selection inputs only. This
    function does not grant Tool access, authorize execution, own budget accounting,
    or own provider session/liveness lifecycle.
    """
    evaluated = sorted(candidates, key=lambda candidate: candidate.provider_id)
    rejected = tuple(
        (candidate.provider_id, reasons)
        for candidate in evaluated
        if (reasons := _rejection_reasons(requirement, candidate))
    )
    rejected_ids = {provider_id for provider_id, _ in rejected}
    eligible = [
        candidate for candidate in evaluated if candidate.provider_id not in rejected_ids
    ]
    provider_ids = tuple(candidate.provider_id for candidate in eligible)
    if not eligible:
        return RoutingDecision(
            status=RoutingStatus.BLOCKED,
            provider_id=None,
            reason="no currently available worker satisfies trusted eligibility constraints",
            eligible_provider_ids=(),
            rejected_reasons=rejected,
        )

    optional_scores = {
        candidate.provider_id: len(
            requirement.optional_capabilities & candidate.reported_capabilities
        )
        for candidate in eligible
    }
    selected = min(
        eligible,
        key=lambda candidate: (-optional_scores[candidate.provider_id], candidate.provider_id),
    )
    return RoutingDecision(
        status=RoutingStatus.SELECTED,
        provider_id=selected.provider_id,
        reason=(
            "selected by required eligibility, optional capability preference, "
            "then provider_id tie-breaker"
        ),
        eligible_provider_ids=provider_ids,
        rejected_reasons=rejected,
    )
