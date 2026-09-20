"""Conservative warm-runtime reuse gate built from provider/runtime facts.

The gate consumes fresh scope, liveness, compatibility, and security-policy
facts. It does not discover resources, poll providers, persist runtime state, or
replace Deep Agents/provider lifecycle APIs.
"""

from dataclasses import dataclass
from enum import StrEnum


class WarmReuseDecision(StrEnum):
    """Decision for whether an existing runtime may be reused."""

    REUSE_ALLOWED = "reuse_allowed"
    REUSE_REJECTED = "reuse_rejected"
    REUSE_UNVERIFIABLE = "reuse_unverifiable"


@dataclass(frozen=True, slots=True)
class WarmReuseFact:
    """Fresh facts required before reusing an existing runtime."""

    scope_matches: bool | None
    live_lookup_supported: bool
    live_exists: bool | None
    compatibility_matches: bool | None
    security_policy_current: bool | None


def warm_reuse_decision(fact: WarmReuseFact) -> WarmReuseDecision:
    """Allow reuse only when every required gate is explicitly satisfied.

    Any explicit mismatch is enough to reject reuse. Missing or unsupported
    provider evidence remains unverifiable rather than being upgraded from a
    persisted resource identity or previous successful use.
    """
    gates = (
        fact.scope_matches,
        fact.live_exists,
        fact.compatibility_matches,
        fact.security_policy_current,
    )

    if any(value is False for value in gates):
        return WarmReuseDecision.REUSE_REJECTED
    if not fact.live_lookup_supported or any(value is None for value in gates):
        return WarmReuseDecision.REUSE_UNVERIFIABLE
    return WarmReuseDecision.REUSE_ALLOWED
