"""Durable Store access for PM memory.

PM memory reuses the project's existing PostgreSQL -> InMemoryStore fallback.
The actual connection string stays owned by the existing environment/config.
"""

from dataclasses import dataclass

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore


@dataclass(frozen=True, slots=True)
class PMMemoryStore:
    """Store plus the durability level currently available to PM memory."""

    store: BaseStore
    backend: str
    durability: str


async def get_pm_memory_store() -> PMMemoryStore:
    """Reuse the project's existing Store provider without creating another one."""
    # Keep the import lazy: the PostgreSQL package is a runtime dependency of the
    # existing db module, while this middleware can still be imported/tested with
    # an in-memory Store.
    from src.deep_agents.db import get_postgres_store

    store = await get_postgres_store()
    if isinstance(store, InMemoryStore):
        return PMMemoryStore(store=store, backend="memory", durability="ephemeral")
    return PMMemoryStore(
        store=store,
        backend=type(store).__name__,
        durability="durable",
    )
