"""Structured project-domain business facts."""

from src.middlewares.project_state.commands import (
    CreateTaskCommand,
    TransitionTaskCommand,
)
from src.middlewares.project_state.models import ProjectSnapshot, Task, TaskStatus
from src.middlewares.project_state.persistence import ProjectStore
from src.middlewares.project_state.runtime import get_project_store
from src.middlewares.project_state.service import ProjectCommand, apply_project_command
from src.middlewares.project_state.transitions import (
    DependencyReadiness,
    TransitionDecision,
    dependency_readiness,
    transition_decision,
    validate_dependency_graph,
)

__all__ = [
    "CreateTaskCommand",
    "DependencyReadiness",
    "ProjectCommand",
    "ProjectStateMiddleware",
    "TransitionFacts",
    "project_context",
    "project_read",
    "ProjectSnapshot",
    "ProjectStore",
    "ProjectArtifacts",
    "ProjectWorkflow",
    "Task",
    "TaskStatus",
    "TransitionDecision",
    "TransitionTaskCommand",
    "apply_project_command",
    "dependency_readiness",
    "get_project_store",
    "transition_decision",
    "validate_dependency_graph",
]


def __getattr__(name):
    """Load the Agent integration only when requested."""
    if name == "ProjectStateMiddleware":
        from .middleware import ProjectStateMiddleware

        return ProjectStateMiddleware
    if name in {"TransitionFacts", "project_context", "project_read"}:
        from . import tools

        return getattr(tools, name)
    if name == "ProjectWorkflow":
        from .workflow import ProjectWorkflow

        return ProjectWorkflow
    if name == "ProjectArtifacts":
        from .artifacts import ProjectArtifacts

        return ProjectArtifacts
    raise AttributeError(name)


# 制品和宿主证据服务按需导出，避免纯领域导入时加载Agent工具。
