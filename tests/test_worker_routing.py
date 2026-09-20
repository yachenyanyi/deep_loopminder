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
    compatibility: dict[str, str] | None = None,
) -> WorkerCandidate:
    return WorkerCandidate(
        provider_id=provider_id,
        worker_kind=kind,
        roles=roles,
        reported_capabilities=capabilities,
        runtime_available=available,
        compatibility=compatibility or {},
    )


def requirement(
    *,
    optional: frozenset[str] = frozenset(),
    constraints: dict[str, str] | None = None,
) -> RoleRequirement:
    return RoleRequirement(
        role="developer",
        required_capabilities=frozenset({"repo.read", "repo.write"}),
        optional_capabilities=optional,
        constraints=constraints or {},
    )


def test_routes_across_native_and_external_workers_without_role_provider_binding() -> None:
    decision = route_worker(
        requirement(),
        [worker("native-dev", kind="native"), worker("external-dev")],
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


def test_unavailable_worker_has_explicit_rejection_diagnostic() -> None:
    decision = route_worker(requirement(), [worker("codex", available=False)])

    assert decision.status is RoutingStatus.BLOCKED
    assert decision.provider_id is None
    assert decision.rejected_reasons == (("codex", ("runtime unavailable",)),)


def test_missing_required_capability_is_diagnosed() -> None:
    decision = route_worker(
        requirement(), [worker("reader", capabilities=frozenset({"repo.read"}))]
    )

    assert decision.status is RoutingStatus.BLOCKED
    assert decision.rejected_reasons == (
        ("reader", ("missing required capabilities: repo.write",)),
    )


def test_wrong_role_blocks_worker_even_when_capabilities_match() -> None:
    decision = route_worker(
        requirement(), [worker("researcher", roles=frozenset({"researcher"}))]
    )

    assert decision.status is RoutingStatus.BLOCKED
    assert "role 'developer' not supported" in decision.rejected_reasons[0][1]


def test_required_capabilities_are_not_transformed_into_authorization_grants() -> None:
    decision = route_worker(requirement(), [worker("codex")])

    assert decision.status is RoutingStatus.SELECTED
    assert not hasattr(decision, "granted_capabilities")
    assert not hasattr(decision, "authorized_tools")


def test_selection_is_deterministic_for_same_current_facts() -> None:
    first = route_worker(requirement(), [worker("zeta"), worker("alpha")])
    second = route_worker(requirement(), [worker("alpha"), worker("zeta")])

    assert first == second
    assert first.provider_id == "alpha"


def test_trusted_compatibility_constraints_filter_without_granting_authority() -> None:
    decision = route_worker(
        requirement(constraints={"platform": "linux"}),
        [
            worker("windows", compatibility={"platform": "windows"}),
            worker("linux", compatibility={"platform": "linux"}),
        ],
    )

    assert decision.provider_id == "linux"
    assert decision.rejected_reasons == (
        ("windows", ("constraint mismatch: platform",)),
    )
    assert not hasattr(decision, "authorized_constraints")


def test_optional_capability_prefers_more_capable_eligible_worker() -> None:
    decision = route_worker(
        requirement(optional=frozenset({"test.execute"})),
        [
            worker("alpha"),
            worker(
                "zeta",
                capabilities=frozenset({"repo.read", "repo.write", "test.execute"}),
            ),
        ],
    )

    assert decision.provider_id == "zeta"
    assert decision.eligible_provider_ids == ("alpha", "zeta")


def test_diagnostics_are_deterministic_and_can_report_multiple_reasons() -> None:
    decision = route_worker(
        requirement(constraints={"platform": "linux"}),
        [
            worker(
                "bad",
                roles=frozenset({"researcher"}),
                capabilities=frozenset({"repo.read"}),
                available=False,
                compatibility={"platform": "windows"},
            )
        ],
    )

    assert decision.rejected_reasons == (
        (
            "bad",
            (
                "runtime unavailable",
                "role 'developer' not supported",
                "missing required capabilities: repo.write",
                "constraint mismatch: platform",
            ),
        ),
    )
