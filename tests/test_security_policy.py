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


def test_child_cannot_widen_parent_security_ceiling():
    deployment = grant("fs:read", "fs:write", "network:https")
    parent = grant("fs:read", "network:https")
    child = grant("fs:read", "fs:write", "network:https", "shell:exec")
    assert effective_grant(deployment, parent, child) == grant("fs:read", "network:https")


def test_empty_intersection_denies_capability():
    effective = effective_grant(grant("fs:read"), grant("network:https"))
    fact = EnforcementFact("filesystem", EnforcementCapability.ENFORCEABLE, mechanism="FilesystemPermission")
    result = authorize(required_capability="fs:read", effective_grant=effective, enforcement=fact)
    assert result.decision is SecurityDecision.DENY


def test_unproven_enforcement_fails_closed_as_capability_gap():
    fact = EnforcementFact(
        "sandbox-network",
        EnforcementCapability.UNSUPPORTED,
        gap="provider exposes no network policy primitive",
    )
    result = authorize(
        required_capability="network:offline",
        effective_grant=grant("network:offline"),
        enforcement=fact,
    )
    assert result.decision is SecurityDecision.CAPABILITY_GAP


def test_partial_enforcement_is_not_promoted_to_guarantee():
    fact = EnforcementFact(
        "llm-transport",
        EnforcementCapability.PARTIAL,
        gap="does not cover shell or MCP egress",
    )
    result = authorize(
        required_capability="network:offline",
        effective_grant=grant("network:offline"),
        enforcement=fact,
    )
    assert result.decision is SecurityDecision.CAPABILITY_GAP


def test_authorized_and_enforceable_allows_execution():
    fact = EnforcementFact(
        "builtin-filesystem",
        EnforcementCapability.ENFORCEABLE,
        mechanism="Deep Agents FilesystemPermission",
    )
    result = authorize(
        required_capability="fs:read",
        effective_grant=grant("fs:read"),
        enforcement=fact,
    )
    assert result.decision is SecurityDecision.ALLOW


def test_enforceable_surface_requires_mechanism():
    with pytest.raises(ValueError, match="mechanism"):
        EnforcementFact("filesystem", EnforcementCapability.ENFORCEABLE)


def test_non_enforceable_surface_requires_explicit_gap():
    with pytest.raises(ValueError, match="gap"):
        EnforcementFact("mcp-network", EnforcementCapability.UNSUPPORTED)
