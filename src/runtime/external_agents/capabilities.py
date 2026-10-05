"""Project negotiated ACP capabilities into eligibility, never authorization."""

from acp.schema import InitializeResponse

from src.runtime.routing.models import WorkerCandidate


class UnsupportedControl(ValueError):
    """A requested operation has no negotiated implementation."""


def negotiated_controls(response: InitializeResponse) -> frozenset[str]:
    """Expose baseline ACP methods and only explicitly advertised extensions."""
    controls = {"ask", "cancel", "new"}
    capabilities = response.agent_capabilities
    if capabilities is None:
        return frozenset(controls)
    if capabilities.load_session:
        controls.add("resume")
    session = capabilities.session_capabilities
    if session:
        for name in ("list", "fork", "close", "resume"):
            if getattr(session, name) is not None:
                controls.add(name)
    return frozenset(controls)


def worker_candidate(
    provider_id: str,
    roles: frozenset[str],
    response: InitializeResponse,
    *,
    business_capabilities: frozenset[str] = frozenset(),
    runtime_available: bool = True,
) -> WorkerCandidate:
    """Combine operator-owned role eligibility with actual negotiated controls.

    ACP does not establish that an agent is competent in Python or testing.
    Business capability labels therefore come from trusted configuration.
    """
    return WorkerCandidate(
        provider_id=provider_id,
        worker_kind="external_acp",
        roles=roles,
        reported_capabilities=business_capabilities
        | frozenset(f"acp.{action}" for action in negotiated_controls(response)),
        runtime_available=runtime_available,
        integration_ref="acp://" + provider_id,
    )
