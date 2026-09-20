"""Provider-neutral adapters from project business facts to routing requests."""

from src.runtime.project.models import Task
from src.runtime.routing.models import RoleRequirement


def role_requirement_from_task(task: Task) -> RoleRequirement:
    """Map Task-owned role/capability facts without introducing provider authority.

    TaskBrief/artifact/execution refs are intentionally ignored: only the structured
    Task business facts owned by the project domain become routing requirements.
    """
    if task.owner_role is None or not task.owner_role.strip():
        raise ValueError("task owner_role is required for worker routing")

    return RoleRequirement(
        role=task.owner_role,
        required_capabilities=task.required_capabilities,
    )
