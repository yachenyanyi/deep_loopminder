"""Project-control PM declarations for issue #38."""

from src.runtime.project.service import ProjectCommand, apply_project_command
from src.runtime.routing import RoleRequirement, role_requirement_from_task

from .context import PMContextProjection, project_pm_context
from .profile import PMProfile, default_pm_profile

# PM mutations intentionally reuse #36's exact typed command/reducer surface.
# These aliases are declarations, not a second mutation API or persistence layer.
pm_project_command = apply_project_command

# PM delegation intentionally reuses #37's exact Task -> RoleRequirement adapter.
# PM declares business requirements; provider selection remains routing-owned.
pm_role_requirement = role_requirement_from_task

__all__ = [
    "PMContextProjection",
    "PMProfile",
    "ProjectCommand",
    "RoleRequirement",
    "default_pm_profile",
    "pm_project_command",
    "pm_role_requirement",
    "project_pm_context",
]
