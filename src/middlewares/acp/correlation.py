"""DeepLoop domain references; provider sessions and history remain provider-owned."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class WorkerRef:
    """Associate a native ACP session with explicit project/task/thread identity."""

    provider_id: str
    session_id: str
    project_id: str
    task_id: str
    thread_id: str
    role: str
    cwd: str
    handoff_id: str | None = None

    def __post_init__(self) -> None:
        """Require complete correlation rather than guessing from transport state."""
        if any(
            not value.strip()
            for value in (
                self.provider_id,
                self.session_id,
                self.project_id,
                self.task_id,
                self.thread_id,
                self.role,
                self.cwd,
            )
        ):
            raise ValueError("worker correlation identities and cwd must be non-empty")

    def to_dict(self) -> dict:
        """Serialize only domain correlations, never provider conversation/state."""
        return asdict(self)
