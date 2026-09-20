"""Project-domain facts that are intentionally separate from execution lifecycle.

These immutable descriptors contain DeepLoopMinder business semantics only.
LangGraph thread/run/checkpoint state and provider/AsyncSubAgent lifecycle remain
owned by their official runtimes and are referenced, never mirrored, here.
"""

from dataclasses import dataclass
from enum import StrEnum


class TaskStatus(StrEnum):
    """Business status; it is not a provider or LangGraph run status."""

    BACKLOG = "backlog"
    READY = "ready"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    DEV_COMPLETE = "dev_complete"
    REVIEW = "review"
    TESTING = "testing"
    FAILED = "failed"
    REWORK = "rework"
    PASSED = "passed"
    WAITING_HUMAN = "waiting_human"
    APPROVED = "approved"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class Task:
    """Structured project task independent from worker execution state."""

    task_id: str
    title: str
    status: TaskStatus
    owner_role: str | None
    required_capabilities: frozenset[str]
    dependencies: frozenset[str]
    acceptance_criteria: tuple[str, ...]
    priority: int = 0
    artifact_refs: tuple[str, ...] = ()
    execution_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate required business identity and task invariants."""
        if not self.task_id.strip():
            raise ValueError("task_id must be non-empty")
        if not self.title.strip():
            raise ValueError("title must be non-empty")
        if not self.acceptance_criteria:
            raise ValueError("acceptance_criteria must be non-empty")
        if self.task_id in self.dependencies:
            raise ValueError("task cannot depend on itself")


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    """Read-only business projection suitable for PM/runtime consumers."""

    project_id: str
    goal: str
    constraints: tuple[str, ...]
    tasks: tuple[Task, ...]

    def __post_init__(self) -> None:
        """Validate project identity and snapshot-level task uniqueness."""
        if not self.project_id.strip():
            raise ValueError("project_id must be non-empty")
        if not self.goal.strip():
            raise ValueError("goal must be non-empty")
        ids = [task.task_id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task_id must be unique within a project snapshot")
