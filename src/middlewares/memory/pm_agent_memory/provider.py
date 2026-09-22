"""PM Memory store provider.

This module intentionally reuses the project's existing durable Store lifecycle.
PM memory code depends on LangGraph's BaseStore, not on PostgreSQL directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from src.deep_agents.db import get_postgres_store


@dataclass(frozen=True, slots=True)
class PMMemoryStoreStatus:
    """Describe the persistence strength currently available to PM memory."""

    backend: str
    durability: str


class PMMemoryStoreProvider:
    """Expose the project's existing Store to PM memory."""

    async def get_store(self) -> BaseStore:
        """Return the existing project Store, including its configured fallback."""
        return await get_postgres_store()

    async def status(self) -> PMMemoryStoreStatus:
        """Return whether PM memory is currently durable or using fallback memory."""
        store = await self.get_store()
        if isinstance(store, InMemoryStore):
            return PMMemoryStoreStatus(backend="memory", durability="degraded")
        return PMMemoryStoreStatus(
            backend=type(store).__name__,
            durability="durable",
        )


pm_memory_store_provider = PMMemoryStoreProvider()
