"""PM-only PostgreSQL lifecycle, without changing the shared Agent runtime."""

import asyncio
import logging
import os
from contextlib import ExitStack, asynccontextmanager

from dotenv import dotenv_values
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from src.middlewares.agent.pm_settings import PMSettings
from src.middlewares.persistence.checkpointer import ThreadOffloadedPostgresSaver

_LOGGER = logging.getLogger(__name__)
_store_ready = False
_checkpoint_ready = None


def database_uri() -> str | None:
    """Read the PM credential, allowing the old URI only before migration."""
    settings = PMSettings()
    value = settings.secret("PM_POSTGRES_URI")
    if value or settings.read().get("migration", {}).get("legacy_imported"):
        return value
    legacy = dotenv_values(settings.env_path, interpolate=False) if settings.env_path.exists() else {}
    return legacy.get("LANGGRAPH_POSTGRES_URI") or os.environ.get("LANGGRAPH_POSTGRES_URI")


def status() -> dict:
    """Expose availability, without returning the database connection string."""
    return {"store_ready": _store_ready, "checkpoint_ready": _checkpoint_ready,
            "configured": bool(database_uri())}


def require_ready():
    """Do not permit real PM runs with a temporary setup-only store."""
    if not _store_ready or _checkpoint_ready is False:
        raise RuntimeError("PM 数据库未连接；请在工作台设置数据库并重启服务")


@asynccontextmanager
async def generate_pm_store():
    """Provide the official Store, or a setup-only store if PostgreSQL is unavailable."""
    global _store_ready
    uri = database_uri()
    _store_ready = False
    if uri:
        from langgraph.store.postgres import PostgresStore

        stack = ExitStack()
        try:
            store = stack.enter_context(PostgresStore.from_conn_string(uri))
            await asyncio.to_thread(store.setup)
        except Exception:
            stack.close()
            _LOGGER.warning("PM PostgreSQL 无法连接；工作台只开放设置。")
        else:
            _store_ready = True
            try:
                yield store
            finally:
                _store_ready = False
                stack.close()
            return
    else:
        _LOGGER.warning("PM PostgreSQL 未配置；工作台只开放设置。")
    yield InMemoryStore()


@asynccontextmanager
async def generate_pm_checkpointer():
    """Use the official saver with the existing Windows event-loop bridge."""
    global _checkpoint_ready
    uri = database_uri()
    _checkpoint_ready = False
    if uri:
        from langgraph.checkpoint.postgres import PostgresSaver

        stack = ExitStack()
        try:
            saver = stack.enter_context(PostgresSaver.from_conn_string(uri))
            await asyncio.to_thread(saver.setup)
        except Exception:
            stack.close()
            _LOGGER.warning("PM 检查点数据库无法连接；工作台只开放设置。")
        else:
            _checkpoint_ready = True
            try:
                yield ThreadOffloadedPostgresSaver(saver)
            finally:
                _checkpoint_ready = False
                stack.close()
            return
    yield InMemorySaver()
