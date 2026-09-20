"""Provider-neutral role and worker descriptors for project routing."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class RoleRequirement:
    """Business role request; capabilities are eligibility requirements, not grants."""

    role: str
    required_capabilities: frozenset[str]
    optional_capabilities: frozenset[str] = frozenset()
    constraints: Mapping[str, str] = MappingProxyType({})

    def __post_init__(self) -> None:
        """Validate stable role and capability identities."""
        if not self.role.strip():
            raise ValueError("role must be non-empty")
        if any(not capability.strip() for capability in self.required_capabilities):
            raise ValueError("required_capabilities must be non-empty strings")
        if any(not capability.strip() for capability in self.optional_capabilities):
            raise ValueError("optional_capabilities must be non-empty strings")


@dataclass(frozen=True, slots=True)
class WorkerCandidate:
    """Current provider/runtime facts consumed by the router without owning lifecycle."""

    provider_id: str
    worker_kind: str
    roles: frozenset[str]
    reported_capabilities: frozenset[str]
    runtime_available: bool
    integration_ref: str | None = None
    compatibility: Mapping[str, str] = MappingProxyType({})

    def __post_init__(self) -> None:
        """Validate stable provider identity and descriptor values."""
        if not self.provider_id.strip():
            raise ValueError("provider_id must be non-empty")
        if not self.worker_kind.strip():
            raise ValueError("worker_kind must be non-empty")
        if any(not role.strip() for role in self.roles):
            raise ValueError("roles must be non-empty strings")
        if any(not capability.strip() for capability in self.reported_capabilities):
            raise ValueError("reported_capabilities must be non-empty strings")
        if any(not key.strip() or not value.strip() for key, value in self.compatibility.items()):
            raise ValueError("compatibility keys and values must be non-empty strings")
