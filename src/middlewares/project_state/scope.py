"""Project identity for production runs and isolated Studio runs."""

import asyncio
import errno
import hashlib
import os
from pathlib import Path
from weakref import WeakKeyDictionary

STUDIO_GRAPH_ID = "pm_agent_studio"
STUDIO_PROJECT_PREFIX = "studio-thread:"
_PROJECT_LOCKS = WeakKeyDictionary()


class _LocalWriterLock:
    """Cooperate across local processes sharing the same application data root."""

    def __init__(self, category, identity):
        self.category, self.identity = category, identity
        self.local = None
        self.file = None
        self.locked = False

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        locks = _PROJECT_LOCKS.setdefault(loop, {})
        key = (self.category, self.identity)
        self.local = locks.setdefault(key, asyncio.Lock())
        await self.local.acquire()
        try:
            base = os.getenv("LOOPMINDER_DATA_DIR") or os.getenv("LOCALAPPDATA")
            root = Path(base) if base else Path.home() / ".local" / "share"
            directory = root / "DeepLoopMinder" / "writer-locks"
            directory.mkdir(parents=True, exist_ok=True)
            filename = hashlib.sha256((self.category + "\0" + self.identity).encode("utf-8")).hexdigest() + ".lock"
            self.file = (directory / filename).open("a+b")
            if self.file.seek(0, os.SEEK_END) == 0:
                self.file.write(b"\0")
                self.file.flush()
            while True:
                try:
                    self.file.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.locked = True
                    return self
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    await asyncio.sleep(0.1)
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            if self.file is not None:
                try:
                    if self.locked:
                        self.file.seek(0)
                        if os.name == "nt":
                            import msvcrt

                            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
                finally:
                    self.file.close()
        finally:
            if self.local is not None and self.local.locked():
                self.local.release()


def project_write_lock(project_id: str) -> _LocalWriterLock:
    """Serialize local writers of a project sharing one application data root."""
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id is required")
    return _LocalWriterLock("project", project_id)


def directory_write_lock(directory_identity: str) -> _LocalWriterLock:
    """Serialize creation and binding of the same canonical directory."""
    if not isinstance(directory_identity, str) or not directory_identity:
        raise ValueError("directory identity is required")
    return _LocalWriterLock("directory", directory_identity)


def resolve_project_id(config: dict) -> str:
    """Keep Studio threads in a reserved namespace and require real project IDs."""
    configurable = config.get("configurable", {})
    if configurable.get("graph_id") == STUDIO_GRAPH_ID:
        thread_id = configurable.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id is required for a Studio test")
        return STUDIO_PROJECT_PREFIX + thread_id
    project_id = configurable.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id is required in config.configurable")
    if project_id.startswith(STUDIO_PROJECT_PREFIX):
        raise ValueError("studio-thread: project IDs are reserved for Studio tests")
    return project_id
