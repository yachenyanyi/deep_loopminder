"""Bind project identities to local directories without changing process cwd.

Callers must serialize mutations, as BaseStore has no portable compare-and-set.
"""

import asyncio
import hashlib
import os
import stat
from pathlib import Path

from langgraph.store.base import BaseStore

_PROJECT_FOLDERS = ("deep_loopminder", "project_folders")


def _resolve_directory(directory: str) -> tuple[str, str]:
    if not isinstance(directory, str) or not directory.strip():
        raise ValueError("请选择项目文件夹")
    path = Path(directory).expanduser()
    if not path.is_absolute():
        raise ValueError("项目文件夹必须使用绝对路径")
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("项目文件夹不存在或无法访问") from exc
    if not path.is_dir():
        raise ValueError("项目路径必须是文件夹")
    normalized = os.path.normcase(str(path))
    return str(path), hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _directory_digest(directory: str) -> str:
    """Hash actual regular files and directory names, failing on unstable input.

    Links and special files are rejected rather than following paths outside the
    project. This is a change detector, not an operating-system read-only lock.
    """
    root = Path(directory)
    digest = hashlib.sha256()

    def visit(path: Path, relative: str) -> None:
        before = path.stat(follow_symlinks=False)
        if (stat.S_ISLNK(before.st_mode)
                or getattr(before, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("项目含链接或重解析点，无法确认审查目录")
        encoded = os.fsencode(relative.replace(os.sep, "/"))
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        if stat.S_ISDIR(before.st_mode):
            digest.update(b"D")
            with os.scandir(path) as iterator:
                names = sorted((entry.name for entry in iterator), key=os.fsencode)
            for name in names:
                visit(path / name, os.path.join(relative, name))
        elif stat.S_ISREG(before.st_mode):
            if before.st_nlink > 1:
                raise ValueError("项目含硬链接，无法确认文件不与其他目录共享")
            digest.update(b"F")
            file_digest = hashlib.sha256()
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as file:
                opened = os.fstat(file.fileno())
                if (opened.st_ino, opened.st_dev, opened.st_size, opened.st_mtime_ns) != (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns):
                    raise ValueError("计算审查版本时项目文件发生变化")
                while chunk := file.read(1024 * 1024):
                    file_digest.update(chunk)
                after_open = os.fstat(file.fileno())
                if (after_open.st_size, after_open.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
                    raise ValueError("计算审查版本时项目文件发生变化")
            digest.update(file_digest.digest())
        else:
            raise ValueError("项目含无法核验的特殊文件")
        after = path.stat(follow_symlinks=False)
        if (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns) != (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns):
            raise ValueError("计算审查版本时项目目录发生变化")

    try:
        visit(root, "")
    except OSError as exc:
        raise ValueError("无法读取完整项目目录，不能确认审查版本") from exc
    return digest.hexdigest()


class ProjectFolders:
    """Project-scoped bindings stored independently from project snapshots."""

    def __init__(self, store: BaseStore):
        """Bind the registry to the host's project Store."""
        self.store = store

    @staticmethod
    async def canonical_directory(directory: str) -> tuple[str, str]:
        """Resolve a folder and its identity for host-side serialization."""
        return await asyncio.to_thread(_resolve_directory, directory)

    async def get(self, project_id: str) -> dict | None:
        """Return the saved directory for a project, if one exists."""
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id is required")
        item = await self.store.aget(_PROJECT_FOLDERS, project_id)
        return dict(item.value) if item else None

    async def verified_binding(self, project_id: str) -> dict:
        """Resolve the binding again before using its directory."""
        binding = await self.get(project_id)
        if binding is None:
            raise ValueError("项目尚未绑定文件夹")
        path, identity = await asyncio.to_thread(_resolve_directory, binding["directory"])
        if identity != binding.get("identity") or os.path.normcase(path) != os.path.normcase(binding["directory"]):
            raise ValueError("项目文件夹身份已变化，请人工核对")
        return binding

    async def find_directory(self, directory: str) -> dict | None:
        """Find an existing project by canonical directory identity."""
        _, identity = await asyncio.to_thread(_resolve_directory, directory)
        offset = 0
        while True:
            items = await self.store.asearch(_PROJECT_FOLDERS, limit=100, offset=offset)
            for item in items:
                if item.value.get("identity") == identity:
                    return dict(item.value)
            if len(items) < 100:
                return None
            offset += len(items)

    async def find_overlapping_directory(self, directory: str, *, exclude_project_id: str | None = None) -> dict | None:
        """Find an existing parent or child binding that shares project files.

        Callers serializing bindings must use one registry-wide lock, because
        different canonical paths can still overlap.
        """
        path, _ = await asyncio.to_thread(_resolve_directory, directory)
        target = os.path.normcase(path)
        offset = 0
        while True:
            items = await self.store.asearch(_PROJECT_FOLDERS, limit=100, offset=offset)
            for item in items:
                if item.value.get("project_id") == exclude_project_id:
                    continue
                saved = item.value.get("directory")
                if not isinstance(saved, str):
                    continue
                existing = os.path.normcase(saved)
                try:
                    shared = os.path.commonpath((target, existing))
                except ValueError:  # Different Windows drives.
                    continue
                if shared in {target, existing}:
                    return dict(item.value)
            if len(items) < 100:
                return None
            offset += len(items)

    async def bind(self, project_id: str, directory: str) -> dict:
        """Bind once; repeat of the same binding is safe under one writer."""
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id is required")
        path, identity = await asyncio.to_thread(_resolve_directory, directory)
        previous = await self.get(project_id)
        if previous:
            if previous.get("identity") == identity:
                return previous
            raise ValueError("项目已绑定其他文件夹；请新建项目")
        other = await self.find_overlapping_directory(path)
        if other:
            raise ValueError(f"文件夹已绑定项目或与其目录重叠: {other['project_id']}")
        binding = {"project_id": project_id, "directory": path, "identity": identity}
        await self.store.aput(_PROJECT_FOLDERS, project_id, binding, index=False)
        return binding

    async def content_digest(self, project_id: str) -> str:
        """Fingerprint the bound folder without storing file contents."""
        binding = await self.get(project_id)
        if binding is None:
            raise ValueError("项目尚未绑定文件夹")
        path, identity = await asyncio.to_thread(_resolve_directory, binding["directory"])
        if identity != binding.get("identity"):
            raise ValueError("项目文件夹身份已变化")
        return await asyncio.to_thread(_directory_digest, path)
