"""Provider-neutral security policy primitives for Issue #20.

Official LangChain/Deep Agents middleware, permissions, HITL, execution
policies, sandboxes, and guardrails remain the enforcement mechanisms. This
module only combines trusted restrictions and records whether a concrete
surface can truthfully enforce a requested policy.
"""

from dataclasses import dataclass
from enum import Enum


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
    enforced_capabilities: frozenset[str]
    mechanism: str | None = None
    gap: str | None = None

    def __post_init__(self) -> None:
        if not self.surface.strip():
            raise ValueError("surface must be non-empty")
        if any(not item.strip() for item in self.enforced_capabilities):
            raise ValueError("enforced_capabilities must contain non-empty strings")
        if self.capability is EnforcementCapability.ENFORCEABLE:
            if not self.mechanism:
                raise ValueError("enforceable surface requires a mechanism")
            if not self.enforced_capabilities:
                raise ValueError("enforceable surface requires enforced capabilities")
        elif not self.gap:
            raise ValueError("partial/unsupported surface requires an explicit gap")


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    decision: SecurityDecision
    reason: str


@dataclass(frozen=True, slots=True)
class EffectiveToolCall:
    """Final execution identity after any HITL edit/resume decision."""

    original_tool: str
    effective_tool: str

    def __post_init__(self) -> None:
        if not self.original_tool.strip() or not self.effective_tool.strip():
            raise ValueError("tool identities must be non-empty")

    @property
    def identity_changed(self) -> bool:
        return self.original_tool != self.effective_tool


def authorize(
    *,
    required_capability: str,
    effective_grant: SecurityGrant,
    enforcement: EnforcementFact,
) -> AuthorizationResult:
    """Fail closed unless both authority and matching enforcement are proven."""

    if not effective_grant.allows(required_capability):
        return AuthorizationResult(SecurityDecision.DENY, "capability_not_granted")
    if enforcement.capability is not EnforcementCapability.ENFORCEABLE:
        return AuthorizationResult(SecurityDecision.CAPABILITY_GAP, "enforcement_unproven")
    if required_capability not in enforcement.enforced_capabilities:
        return AuthorizationResult(SecurityDecision.CAPABILITY_GAP, "surface_does_not_enforce_capability")
    return AuthorizationResult(SecurityDecision.ALLOW, "authorized_and_enforceable")


def authorize_effective_tool_call(
    *,
    call: EffectiveToolCall,
    required_capability: str,
    effective_grant: SecurityGrant,
    enforcement: EnforcementFact,
    allow_cross_tool_edit: bool = False,
    target_human_approval_proven: bool = False,
    target_requires_human_approval: bool = False,
) -> AuthorizationResult:
    """Authorize the final tool identity rather than trusting an earlier review.

    This does not implement HITL or resume. It is a boundary invariant for the
    execution adapter: a human review of Tool A is not authority for edited
    Tool B. If cross-tool editing is disabled, identity changes fail closed. If
    it is enabled and the target requires human approval, current target
    approval must be independently proven before normal capability checks.
    """

    if call.identity_changed and not allow_cross_tool_edit:
        return AuthorizationResult(SecurityDecision.DENY, "cross_tool_edit_forbidden")
    if call.identity_changed and target_requires_human_approval and not target_human_approval_proven:
        return AuthorizationResult(SecurityDecision.DENY, "target_human_approval_unproven")
    return authorize(
        required_capability=required_capability,
        effective_grant=effective_grant,
        enforcement=enforcement,
    )


def effective_grant(
    deployment: SecurityGrant,
    *more_specific_restrictions: SecurityGrant,
) -> SecurityGrant:
    """Intersect deployment/project/parent/run/task/child restrictions."""

    return deployment.narrow(*more_specific_restrictions)
