"""Project-level default discussion mode per Worker provider.

A saved preference is only a default. Each discussion records the mode it
actually requested and what the adapter reported, so later edits to the
default never rewrite history.
"""

from langgraph.store.base import BaseStore

_NAMESPACE = ("deep_loopminder", "project_worker_modes")
_PROVIDERS = frozenset({"claude", "codex"})


class ProjectWorkerModes:
    """Store the preferred mode for one project and provider."""

    def __init__(self, store: BaseStore):
        """Bind preferences to the host Store."""
        self.store = store

    async def get(self, project_id: str, provider_id: str) -> dict | None:
        """Return the saved preference, or None when nothing was saved."""
        if provider_id not in _PROVIDERS:
            raise ValueError("未知的 Worker 提供方")
        item = await self.store.aget(_NAMESPACE, f"{project_id}/{provider_id}")
        return dict(item.value) if item else None

    async def all_for_project(self, project_id: str) -> dict:
        """Return saved preferences keyed by provider."""
        saved = {}
        for provider_id in sorted(_PROVIDERS):
            value = await self.get(project_id, provider_id)
            if value is not None:
                saved[provider_id] = value
        return saved

    async def save(self, project_id: str, provider_id: str, mode_id: str) -> dict:
        """Save a target mode; only the ID is kept, never an inferred list."""
        if provider_id not in _PROVIDERS:
            raise ValueError("未知的 Worker 提供方")
        if not isinstance(mode_id, str) or not mode_id.strip() or len(mode_id) > 128:
            raise ValueError("模式标识无效")
        value = {"project_id": project_id, "provider_id": provider_id, "mode_id": mode_id.strip()}
        await self.store.aput(_NAMESPACE, f"{project_id}/{provider_id}", value, index=False)
        return value
