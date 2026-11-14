"""Project-scoped paths for PM and Worker trajectory recordings."""

import hashlib
import os
from pathlib import Path


def trajectory_directory(project_id: str) -> Path:
    """Map a project ID to a safe, separate directory without using it as a path."""
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id is required")
    base = os.getenv("LOOPMINDER_DATA_DIR") or os.getenv("LOCALAPPDATA")
    if not base:
        raise ValueError("未配置本机应用数据目录")
    identity = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
    return Path(base) / "DeepLoopMinder" / "trajectories" / identity
