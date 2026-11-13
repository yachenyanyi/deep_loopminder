"""Typed project-domain commands.

Commands express DeepLoop business mutations only. They do not mirror
LangGraph run/thread/checkpoint state or provider worker lifecycle.
"""

from dataclasses import dataclass

from src.middlewares.project_state.models import Task, TaskStatus


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


@dataclass(frozen=True, slots=True)
class AttachTaskEvidenceCommand:
    """由可信运行集成附加来源引用，不向模型暴露任意Run绑定。"""

    project_id: str
    task_id: str
    execution_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplaceTaskBriefCommand:
    """修复任务说明引用，历史运行和结果保留，旧验收自动因版本变化失效。"""

    project_id: str
    task_id: str
    task_brief_ref: str


@dataclass(frozen=True, slots=True)
class UpdateTaskDependenciesCommand:
    """仅修正尚未执行任务的依赖，不以重建同名任务覆盖历史。"""

    project_id: str
    task_id: str
    dependencies: frozenset[str]


@dataclass(frozen=True, slots=True)
class UpdateTaskCapabilitiesCommand:
    """更正尚未执行任务的能力要求。

    Capabilities are matched by exact set inclusion against what a provider
    reports, so a requirement outside the provider vocabulary is unanswerable.
    Correcting one before execution does not rewrite run history.
    """

    project_id: str
    task_id: str
    required_capabilities: frozenset[str]

    def __post_init__(self) -> None:
        """Reject empty identities here; the reducer checks the vocabulary."""
        if not self.project_id.strip():
            raise ValueError("project_id must be non-empty")
        if not self.task_id.strip():
            raise ValueError("task_id must be non-empty")
        if any(not capability.strip() for capability in self.required_capabilities):
            raise ValueError("required_capabilities must be non-empty strings")


@dataclass(frozen=True, slots=True)
class UpdateTaskOwnerRoleCommand:
    """更正尚未执行任务的负责人角色。

    ``owner_role`` is a routing key, so a display name in that field makes the
    task unroutable and permanently blocked. Correcting it before execution does
    not rewrite run history; after execution starts the record belongs to the
    attempt that actually ran, so the command refuses.
    """

    project_id: str
    task_id: str
    owner_role: str

    def __post_init__(self) -> None:
        """Reject empty identities here; the reducer checks the role set."""
        if not self.project_id.strip():
            raise ValueError("project_id must be non-empty")
        if not self.task_id.strip():
            raise ValueError("task_id must be non-empty")
        if not self.owner_role.strip():
            raise ValueError("owner_role must be non-empty")


@dataclass(frozen=True, slots=True)
class RestoreTaskDependenciesCommand:
    """宿主依据历史证据恢复误删边，不删除依赖、不重写运行历史，不向模型暴露。"""

    project_id: str
    task_id: str
    dependencies: frozenset[str]
    evidence_ref: str
