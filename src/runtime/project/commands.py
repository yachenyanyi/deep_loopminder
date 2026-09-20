"""Typed project-domain commands.

Commands express DeepLoop business mutations only. They do not mirror
LangGraph run/thread/checkpoint state or provider worker lifecycle.
"""

from dataclasses import dataclass

from src.runtime.project.models import Task, TaskStatus


@dataclass(frozen=True, slots=True)
class CreateTaskCommand:
    """Create a structured Task together with its bounded TaskBrief ref."""

    project_id: str
    task: Task
    task_brief_ref: str

    def __post_init__(self) -> None:
        """Require stable project and TaskBrief identities."""
        if not self.project_id.strip():
            raise ValueError("project_id must be non-empty")
        if not self.task_brief_ref.strip():
            raise ValueError("task_brief_ref must be non-empty")


@dataclass(frozen=True, slots=True)
class TransitionTaskCommand:
    """Request a validated project-business status transition."""

    project_id: str
    task_id: str
    target_status: TaskStatus
    validation_accepted: bool | None = None
    human_approved: bool | None = None

    def __post_init__(self) -> None:
        """Require stable project and task identities."""
        if not self.project_id.strip():
            raise ValueError("project_id must be non-empty")
        if not self.task_id.strip():
            raise ValueError("task_id must be non-empty")
