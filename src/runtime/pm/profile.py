"""Declarative PM profile for the Project Control Agent.

This module intentionally does not assemble an agent or mutate middleware lists.
The profile is input for the #21 assembly boundary; execution capabilities stay
with workers selected by #37.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PMProfile:
    """Stable, provider-neutral requirements for the project-control role."""

    role: str
    capabilities: frozenset[str]
    context_policy: str
    memory_policy: str
    budget_policy: str
    approval_policy: str
    middleware_profile: tuple[str, ...] = ()


_PM_CONTROL_CAPABILITIES = frozenset(
    {
        "project.read",
        "project.command",
        "project.plan",
        "project.replan",
        "project.delegate.role_requirement",
        "project.request_human_decision",
    }
)

_FORBIDDEN_EXECUTION_CAPABILITIES = frozenset(
    {
        "repo.write.source",
        "shell.execute",
        "test.execute",
        "provider.select",
        "provider.session.control",
    }
)


def default_pm_profile() -> PMProfile:
    """Return the stable PM declaration without provider or worker execution power."""
    profile = PMProfile(
        role="project_manager",
        capabilities=_PM_CONTROL_CAPABILITIES,
        context_policy="pm_project_projection",
        memory_policy="validated_project_memory",
        budget_policy="pm_control_budget",
        approval_policy="human_boundary",
    )
    forbidden = profile.capabilities & _FORBIDDEN_EXECUTION_CAPABILITIES
    if forbidden:
        raise ValueError(f"PM profile contains worker execution capabilities: {sorted(forbidden)}")
    return profile
