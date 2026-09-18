"""Provider-neutral security policy primitives for Issue #20.

Official LangChain/Deep Agents middleware, permissions, HITL, execution
policies, sandboxes, and guardrails remain the enforcement mechanisms. This
module only combines trusted restrictions and records whether a concrete
surface can truthfully enforce a requested policy.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class EnforcementCapability(str, Enum):
    ENFORCEABLE = "enforceable"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class SecurityDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    CAPABILITY_GAP = "capability_gap"


@dataclass(frozen=True, slots=True)
class SecurityGrant:
    """A trusted set of capabilities; children may only narrow it."""

    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        if any(not capability.strip() for capability in self.capabilities):
            raise ValueError("capabilities must be non-empty strings")

    def narrow(self, *restrictions: "SecurityGrant") -> "SecurityGrant":
        effective = self.capabilities
        for restriction in restrictions:
            effective = effective.intersection(restriction.capabilities)
        return SecurityGrant(frozenset(effective))

    def allows(self, capability: str) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True, slots=True)
class EnforcementFact:
    """Trusted adapter/runtime fact for one concrete security surface."""

    surface: str
    capability: EnforcementCapability
    mechanism: str | None = None
    gap: str | None = None

    def __post_init__(self) -> None:
        if not self.surface.strip():
            raise ValueError("surface must be non-empty")
        if self.capability is EnforcementCapability.ENFORCEABLE and not self.mechanism:
            raise ValueError("enforceable surface requires a mechanism")
        if self.capability is not EnforcementCapability.ENFORCEABLE and not self.gap:
            raise ValueError("partial/unsupported surface requires an explicit gap")


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    decision: SecurityDecision
    reason: str


def authorize(
    *,
    required_capability: str,
    effective_grant: SecurityGrant,
    enforcement: EnforcementFact,
) -> AuthorizationResult:
    """Fail closed unless both authority and enforcement are currently proven."""

    if not effective_grant.allows(required_capability):
        return AuthorizationResult(SecurityDecision.DENY, "capability_not_granted")
    if enforcement.capability is not EnforcementCapability.ENFORCEABLE:
        return AuthorizationResult(SecurityDecision.CAPABILITY_GAP, "enforcement_unproven")
    return AuthorizationResult(SecurityDecision.ALLOW, "authorized_and_enforceable")


def effective_grant(
    deployment: SecurityGrant,
    *more_specific_restrictions: SecurityGrant,
) -> SecurityGrant:
    """Intersect deployment/project/parent/run/task/child restrictions."""

    return deployment.narrow(*more_specific_restrictions)
