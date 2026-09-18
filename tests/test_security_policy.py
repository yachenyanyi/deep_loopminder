import pytest

from src.middlewares.execution.security_policy import (
    EnforcementCapability,
    EnforcementFact,
    SecurityDecision,
    SecurityGrant,
    authorize,
    effective_grant,
)


def grant(*capabilities: str) -> SecurityGrant:
    return SecurityGrant(frozenset(capabilities))


def fact(surface, capability, *enforced, mechanism=None, gap=None):
    return EnforcementFact(surface, capability, frozenset(enforced), mechanism, gap)


def test_child_cannot_widen_parent_security_ceiling():
    deployment = grant("fs:read", "fs:write", "network:https")
    parent = grant("fs:read", "network:https")
    child = grant("fs:read", "fs:write", "network:https", "shell:exec")
    assert effective_grant(deployment, parent, child) == grant("fs:read", "network:https")


def test_empty_intersection_denies_capability():
    effective = effective_grant(grant("fs:read"), grant("network:https"))
    enforcement = fact("filesystem", EnforcementCapability.ENFORCEABLE, "fs:read", mechanism="FilesystemPermission")
    result = authorize(required_capability="fs:read", effective_grant=effective, enforcement=enforcement)
    assert result.decision is SecurityDecision.DENY


def test_unproven_enforcement_fails_closed_as_capability_gap():
    enforcement = fact(
        "sandbox-network", EnforcementCapability.UNSUPPORTED,
        gap="provider exposes no network policy primitive",
    )
    result = authorize(
        required_capability="network:offline",
        effective_grant=grant("network:offline"),
        enforcement=enforcement,
    )
    assert result.decision is SecurityDecision.CAPABILITY_GAP


def test_partial_enforcement_is_not_promoted_to_guarantee():
    enforcement = fact(
        "llm-transport", EnforcementCapability.PARTIAL, "network:offline",
        gap="does not cover shell or MCP egress",
    )
    result = authorize(
        required_capability="network:offline",
        effective_grant=grant("network:offline"),
        enforcement=enforcement,
    )
    assert result.decision is SecurityDecision.CAPABILITY_GAP


def test_enforceable_surface_only_authorizes_capabilities_it_covers():
    enforcement = fact(
        "builtin-filesystem", EnforcementCapability.ENFORCEABLE, "fs:read",
        mechanism="Deep Agents FilesystemPermission",
    )
    result = authorize(
        required_capability="network:offline",
        effective_grant=grant("network:offline"),
        enforcement=enforcement,
    )
    assert result.decision is SecurityDecision.CAPABILITY_GAP
    assert result.reason == "surface_does_not_enforce_capability"


def test_authorized_and_matching_enforcement_allows_execution():
    enforcement = fact(
        "builtin-filesystem", EnforcementCapability.ENFORCEABLE, "fs:read",
        mechanism="Deep Agents FilesystemPermission",
    )
    result = authorize(
        required_capability="fs:read",
        effective_grant=grant("fs:read"),
        enforcement=enforcement,
    )
    assert result.decision is SecurityDecision.ALLOW


def test_enforceable_surface_requires_mechanism_and_capability_scope():
    with pytest.raises(ValueError, match="mechanism"):
        fact("filesystem", EnforcementCapability.ENFORCEABLE, "fs:read")
    with pytest.raises(ValueError, match="enforced capabilities"):
        fact("filesystem", EnforcementCapability.ENFORCEABLE, mechanism="FilesystemPermission")


def test_non_enforceable_surface_requires_explicit_gap():
    with pytest.raises(ValueError, match="gap"):
        fact("mcp-network", EnforcementCapability.UNSUPPORTED)
