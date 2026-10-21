"""PM 公共接口；所有 PM 实现集中在 agent.py。"""

from .agent import (
    PMContextProjection,
    PMProfile,
    ProjectCommand,
    RoleRequirement,
    assemble_pm_user_middleware,
    default_pm_profile,
    pm_agent,
    pm_context_projection,
    pm_project_command,
    pm_role_requirement,
    project_pm_context,
    stable_pm_system_prompt,
)

__all__ = [
    "PMContextProjection",
    "PMProfile",
    "ProjectCommand",
    "RoleRequirement",
    "default_pm_profile",
    "assemble_pm_user_middleware",
    "pm_agent",
    "pm_context_projection",
    "pm_project_command",
    "pm_role_requirement",
    "project_pm_context",
    "stable_pm_system_prompt",
]
