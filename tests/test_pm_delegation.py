from src.runtime.pm import pm_role_requirement
from src.runtime.project.models import Task, TaskStatus
from src.runtime.routing import role_requirement_from_task


def test_pm_delegation_reuses_provider_neutral_routing_adapter() -> None:
    assert pm_role_requirement is role_requirement_from_task

    task = Task(
        task_id="task-1",
        title="Implement feature",
        status=TaskStatus.READY,
        owner_role="developer",
        required_capabilities=frozenset({"repo.read", "repo.write.source"}),
        dependencies=frozenset(),
        acceptance_criteria=("tests pass",),
        artifact_refs=("taskbrief://task-1",),
        execution_refs=("thread://opaque",),
    )

    requirement = pm_role_requirement(task)

    assert requirement.role == "developer"
    assert requirement.required_capabilities == frozenset(
        {"repo.read", "repo.write.source"}
    )
    assert not hasattr(requirement, "provider_id")
    assert not hasattr(requirement, "authorized_tools")
