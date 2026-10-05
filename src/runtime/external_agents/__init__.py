"""External Worker Control Plane using official ACP SDK and adapters."""

from .capabilities import UnsupportedControl, negotiated_controls, worker_candidate
from .correlation import WorkerRef

__all__ = [
    "ACPWorker",
    "UnsupportedControl",
    "WorkerRef",
    "negotiated_controls",
    "worker_candidate",
]


def __getattr__(name):
    """Keep CLI/package discovery independent of legacy middleware startup."""
    if name == "ACPWorker":
        from .control import ACPWorker

        return ACPWorker
    raise AttributeError(name)
