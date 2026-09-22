"""Durable store access for PM memory.

The PM memory layer reuses the project's existing PostgreSQL -> InMemoryStore
fallback. It does not own connection strings or create another store runtime.
"""

from dataclasses import dataclass

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from src.deep_agents.db import get_postgres_store


@dataclass(frozen=True, slots=True)
class PMMemoryStore:
    """The store PM memory should use for this process."""

    store: BaseStore
    backend: str
    durability: str


async def get_pm_memory_store() -> PMMemoryStore:
    """Return the existing project store and make fallback durability visible."""
    store = await get_postgres_store()
    if isinstance(store, InMemoryStore):
        return PMMemoryStore(store=store, backend="memory", durability="ephemeral")
    return PMMemoryStore(
        store=store,
        backend=type(store).__name__,
        durability="durable",
    )
