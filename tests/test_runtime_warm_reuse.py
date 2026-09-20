from src.runtime.warm_reuse import (
    WarmReuseDecision,
    WarmReuseFact,
    warm_reuse_decision,
)


def test_warm_reuse_requires_all_current_facts() -> None:
    decision = warm_reuse_decision(
        WarmReuseFact(
            scope_matches=True,
            live_lookup_supported=True,
            live_exists=True,
            compatibility_matches=True,
            security_policy_current=True,
        )
    )

    assert decision is WarmReuseDecision.REUSE_ALLOWED


def test_scope_mismatch_rejects_reuse_without_needing_other_inference() -> None:
    decision = warm_reuse_decision(
        WarmReuseFact(
            scope_matches=False,
            live_lookup_supported=False,
            live_exists=None,
            compatibility_matches=None,
            security_policy_current=None,
        )
    )

    assert decision is WarmReuseDecision.REUSE_REJECTED


def test_persisted_identity_cannot_replace_fresh_liveness_lookup() -> None:
    decision = warm_reuse_decision(
        WarmReuseFact(
            scope_matches=True,
            live_lookup_supported=False,
            live_exists=True,
            compatibility_matches=True,
            security_policy_current=True,
        )
    )

    assert decision is WarmReuseDecision.REUSE_UNVERIFIABLE


def test_unknown_compatibility_keeps_reuse_unverifiable() -> None:
    decision = warm_reuse_decision(
        WarmReuseFact(
            scope_matches=True,
            live_lookup_supported=True,
            live_exists=True,
            compatibility_matches=None,
            security_policy_current=True,
        )
    )

    assert decision is WarmReuseDecision.REUSE_UNVERIFIABLE


def test_stale_security_policy_rejects_reuse() -> None:
    decision = warm_reuse_decision(
        WarmReuseFact(
            scope_matches=True,
            live_lookup_supported=True,
            live_exists=True,
            compatibility_matches=True,
            security_policy_current=False,
        )
    )

    assert decision is WarmReuseDecision.REUSE_REJECTED
