import pytest
from deepagents import FilesystemPermission

from src.middlewares.execution.security_policy import (
    EffectiveToolCall,
    EnforcementCapability,
    EnforcementFact,
    SecurityDecision,
    SecurityGrant,
    authorize,
    authorize_effective_tool_call,
    effective_grant,
    scoped_filesystem_permissions,
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


def test_cross_tool_hitl_edit_is_denied_by_default():
    result = authorize_effective_tool_call(
        call=EffectiveToolCall("tool_a", "tool_b"),
        required_capability="tool:b",
        effective_grant=grant("tool:b"),
        enforcement=fact("tool-boundary", EnforcementCapability.ENFORCEABLE, "tool:b", mechanism="wrap_tool_call"),
    )
    assert result.decision is SecurityDecision.DENY
    assert result.reason == "cross_tool_edit_forbidden"


def test_cross_tool_edit_cannot_reuse_original_human_review():
    result = authorize_effective_tool_call(
        call=EffectiveToolCall("tool_a", "tool_b"),
        required_capability="tool:b",
        effective_grant=grant("tool:b"),
        enforcement=fact("tool-boundary", EnforcementCapability.ENFORCEABLE, "tool:b", mechanism="wrap_tool_call"),
        allow_cross_tool_edit=True,
        target_requires_human_approval=True,
        target_human_approval_proven=False,
    )
    assert result.decision is SecurityDecision.DENY
    assert result.reason == "target_human_approval_unproven"


def test_cross_tool_edit_uses_target_capability_not_original_capability():
    result = authorize_effective_tool_call(
        call=EffectiveToolCall("tool_a", "tool_b"),
        required_capability="tool:b",
        effective_grant=grant("tool:a"),
        enforcement=fact("tool-boundary", EnforcementCapability.ENFORCEABLE, "tool:b", mechanism="wrap_tool_call"),
        allow_cross_tool_edit=True,
    )
    assert result.decision is SecurityDecision.DENY
    assert result.reason == "capability_not_granted"


def test_cross_tool_edit_can_proceed_only_after_target_policy_is_proven():
    result = authorize_effective_tool_call(
        call=EffectiveToolCall("tool_a", "tool_b"),
        required_capability="tool:b",
        effective_grant=grant("tool:b"),
        enforcement=fact("tool-boundary", EnforcementCapability.ENFORCEABLE, "tool:b", mechanism="wrap_tool_call"),
        allow_cross_tool_edit=True,
        target_requires_human_approval=True,
        target_human_approval_proven=True,
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


def test_official_filesystem_permissions_close_permissive_default():
    permissions = scoped_filesystem_permissions("/workspace")

    assert permissions[0].mode == "allow"
    assert permissions[0].paths == ["/workspace/**"]
    assert permissions[-1].mode == "deny"
    assert permissions[-1].paths == ["/**"]


def test_scoped_filesystem_permissions_reject_untrusted_path_patterns():
    for prefix in ("workspace", "/workspace/*", "/workspace/[ab]"):
        with pytest.raises(ValueError, match="absolute non-glob"):
            scoped_filesystem_permissions(prefix)


def test_official_filesystem_permission_is_not_custom_tool_authority():
    filesystem_enforcement = fact(
        "builtin-filesystem",
        EnforcementCapability.ENFORCEABLE,
        "fs:read",
        "fs:write",
        mechanism="Deep Agents FilesystemPermission",
    )

    result = authorize(
        required_capability="tool:custom-admin",
        effective_grant=grant("tool:custom-admin"),
        enforcement=filesystem_enforcement,
    )

    assert result.decision is SecurityDecision.CAPABILITY_GAP
    assert result.reason == "surface_does_not_enforce_capability"
