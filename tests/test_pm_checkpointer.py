"""PM 会话检查点接入 PostgreSQL 的官方 custom checkpointer。

官方写法（langsmith/custom-checkpointer）：``checkpointer.path`` 指向 async
上下文管理器，yield BaseCheckpointSaver。这里验证工厂行为、langgraph.json
配置的官方 schema 符合性，以及线程卸载包装对真库的读写往返；连接测试按需
跳过（无数据库配置时）。
"""

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

import pytest

from src.middlewares.persistence.checkpointer import (
    ThreadOffloadedPostgresSaver,
    generate_pm_checkpointer,
)

_ROOT = Path(__file__).parents[1]
_CONFIG = json.loads((_ROOT / "langgraph.json").read_text(encoding="utf-8"))

_URIS = ("LANGGRAPH_POSTGRES_URI is not set", "ephemeral")


def _pg_uri():
    """The URI from env or .env, for the connection-level tests only."""
    uri = os.getenv("LANGGRAPH_POSTGRES_URI")
    if uri:
        return uri
    env_file = _ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("LANGGRAPH_POSTGRES_URI="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def test_langgraph_json_uses_official_custom_checkpointer_schema():
    """The official schema has no type/connection_string fields; a path-based
    custom checkpointer is the documented way to pick the storage backend."""
    checkpointer = _CONFIG["checkpointer"]
    assert "path" in checkpointer and checkpointer["path"].endswith(":generate_pm_checkpointer")
    assert "type" not in checkpointer and "connection_string" not in checkpointer
    # The project Store stays exactly where it was; checkpoint storage must
    # not move or replace it.
    assert _CONFIG["store"]["path"].endswith(":generate_pm_store")


def test_factory_falls_back_to_memory_without_database(monkeypatch, caplog):
    import langgraph.checkpoint.memory as memory

    monkeypatch.delenv("LANGGRAPH_POSTGRES_URI", raising=False)

    async def scenario():
        async with generate_pm_checkpointer() as saver:
            return saver

    with caplog.at_level(logging.WARNING, logger="src.middlewares.persistence.checkpointer"):
        assert isinstance(asyncio.run(scenario()), memory.InMemorySaver)
    # The fallback must be announced, not silent: this mode loses everything
    # on restart and the operator has to see that.
    text = caplog.text
    for fragment in _URIS:
        assert fragment in text


@pytest.mark.skipif(_pg_uri() is None, reason="LANGGRAPH_POSTGRES_URI 未配置")
def test_factory_wraps_official_sync_postgres_saver(monkeypatch):
    from langgraph.checkpoint.postgres import PostgresSaver

    monkeypatch.setenv("LANGGRAPH_POSTGRES_URI", _pg_uri())

    async def scenario():
        async with generate_pm_checkpointer() as saver:
            assert isinstance(saver, ThreadOffloadedPostgresSaver)
            assert isinstance(saver._inner, PostgresSaver)
            # setup() creates/migrates its own tables; running it twice is safe
            # and must not touch the project Store's data.
            saver._inner.setup()
            saver._inner.setup()

    asyncio.run(scenario())


@pytest.mark.skipif(_pg_uri() is None, reason="LANGGRAPH_POSTGRES_URI 未配置")
def test_wrapper_round_trip_put_and_get_tuple_then_cleanup(monkeypatch):
    """The async surface the runtime calls must persist and read back real
    checkpoints through the thread offload, then clean its own rows up."""
    from langgraph.checkpoint.base import Checkpoint, create_checkpoint

    monkeypatch.setenv("LANGGRAPH_POSTGRES_URI", _pg_uri())
    thread_id = "test-pm-checkpointer-" + uuid.uuid4().hex[:12]
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": None}}

    async def scenario():
        async with generate_pm_checkpointer() as saver:
            # create_checkpoint copies channel_versions from a base checkpoint;
            # start from an empty one.
            base = Checkpoint(
                v=1, ts="", id="0" * 32, channel_values={}, channel_versions={},
                versions_seen={}, pending_sends=[], updated_channels=None,
            )
            checkpoint = create_checkpoint(base, None, 1)
            checkpoint["channel_values"]["messages"] = []
            saved = await saver.aput(config, checkpoint, {"source": "test"}, {})
            assert saved["configurable"]["checkpoint_id"]
            got = await saver.aget_tuple(config)
            assert got is not None
            assert got.config["configurable"]["thread_id"] == thread_id
            await saver.adelete_thread(thread_id)
            assert await saver.aget_tuple(config) is None

    asyncio.run(scenario())
