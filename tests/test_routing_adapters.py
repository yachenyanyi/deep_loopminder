import pytest

from src.runtime.project.models import Task, TaskStatus
from src.runtime.routing.adapters import role_requirement_from_task


def task(*, owner_role: str | None = "developer") -> Task:
    return Task(
        task_id="task-1",
        title="Implement feature",
        status=TaskStatus.READY,
        owner_role=owner_role,
        required_capabilities=frozenset({"repo.read", "repo.write.source"}),
        dependencies=frozenset(),
        acceptance_criteria=("tests pass",),
        artifact_refs=("taskbrief://task-1",),
        execution_refs=("thread://opaque",),
    )


def test_task_maps_only_structured_role_and_required_capabilities() -> None:
    requirement = role_requirement_from_task(task())

    assert requirement.role == "developer"
    assert requirement.required_capabilities == frozenset(
        {"repo.read", "repo.write.source"}
    )
    assert requirement.optional_capabilities == frozenset()
    assert dict(requirement.constraints) == {}


def test_task_refs_do_not_become_provider_or_authorization_constraints() -> None:
    requirement = role_requirement_from_task(task())

    assert not hasattr(requirement, "provider_id")
    assert not hasattr(requirement, "artifact_refs")
    assert not hasattr(requirement, "execution_refs")
    assert not hasattr(requirement, "authorized_tools")


def test_task_without_owner_role_fails_closed_for_worker_routing() -> None:
    with pytest.raises(ValueError, match="owner_role"):
        role_requirement_from_task(task(owner_role=None))
