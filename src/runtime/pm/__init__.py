"""Project-control PM declarations for issue #38."""

from src.runtime.project.service import ProjectCommand, apply_project_command

from .context import PMContextProjection, project_pm_context
from .profile import PMProfile, default_pm_profile

# PM mutations intentionally reuse #36's exact typed command/reducer surface.
# These aliases are declarations, not a second mutation API or persistence layer.
pm_project_command = apply_project_command

__all__ = [
    "PMContextProjection",
    "PMProfile",
    "ProjectCommand",
    "default_pm_profile",
    "pm_project_command",
    "project_pm_context",
]
