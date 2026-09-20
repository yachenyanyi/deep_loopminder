from src.runtime.routing import (
    RoleRequirement,
    RoutingStatus,
    WorkerCandidate,
    route_worker,
)


def worker(
    provider_id: str,
    *,
    kind: str = "external",
    roles: frozenset[str] = frozenset({"developer"}),
    capabilities: frozenset[str] = frozenset({"repo.read", "repo.write"}),
    available: bool = True,
) -> WorkerCandidate:
    return WorkerCandidate(
        provider_id=provider_id,
        worker_kind=kind,
        roles=roles,
        reported_capabilities=capabilities,
        runtime_available=available,
    )


def requirement() -> RoleRequirement:
    return RoleRequirement(
        role="developer",
        required_capabilities=frozenset({"repo.read", "repo.write"}),
    )


def test_routes_across_native_and_external_workers_without_role_provider_binding() -> None:
    decision = route_worker(
        requirement(),
        [
            worker("native-dev", kind="native"),
            worker("external-dev", kind="external"),
        ],
    )

    assert decision.status is RoutingStatus.SELECTED
    assert decision.provider_id == "external-dev"
    assert decision.eligible_provider_ids == ("external-dev", "native-dev")


def test_provider_replacement_does_not_change_role_requirement() -> None:
    request = requirement()

    claude = route_worker(request, [worker("claude")])
    codex = route_worker(request, [worker("codex")])

    assert claude.provider_id == "claude"
    assert codex.provider_id == "codex"
    assert request.role == "developer"
    assert request.required_capabilities == frozenset({"repo.read", "repo.write"})


def test_unavailable_worker_is_not_selected_from_stale_capability_snapshot() -> None:
    decision = route_worker(requirement(), [worker("codex", available=False)])

    assert decision.status is RoutingStatus.BLOCKED
    assert decision.provider_id is None
    assert decision.eligible_provider_ids == ()


def test_missing_required_capability_blocks_worker() -> None:
    decision = route_worker(
        requirement(),
        [worker("reader", capabilities=frozenset({"repo.read"}))],
    )

    assert decision.status is RoutingStatus.BLOCKED
    assert "required capabilities" in decision.reason


def test_wrong_role_blocks_worker_even_when_capabilities_match() -> None:
    decision = route_worker(
        requirement(),
        [worker("researcher", roles=frozenset({"researcher"}))],
    )

    assert decision.status is RoutingStatus.BLOCKED


def test_required_capabilities_are_not_transformed_into_authorization_grants() -> None:
    request = requirement()
    candidate = worker("codex")

    decision = route_worker(request, [candidate])

    assert decision.status is RoutingStatus.SELECTED
    assert not hasattr(decision, "granted_capabilities")
    assert not hasattr(decision, "authorized_tools")


def test_selection_is_deterministic_for_same_current_facts() -> None:
    first = route_worker(requirement(), [worker("zeta"), worker("alpha")])
    second = route_worker(requirement(), [worker("alpha"), worker("zeta")])

    assert first == second
    assert first.provider_id == "alpha"
