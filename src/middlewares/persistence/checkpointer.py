"""官方 custom checkpointer：把 PM 会话检查点接入项目 PostgreSQL。

langgraph dev 默认把检查点放在内存里（重启即丢）。官方 Agent Server 支持
``checkpointer.path`` 指向一个 async 上下文管理器、yield 一个
BaseCheckpointSaver（langsmith/custom-checkpointer）。

为什么包一层线程卸载而不是直接用官方 AsyncPostgresSaver：psycopg 的异步模式
在 Windows 上强制要求 SelectorEventLoop（连接即报错），而本服务的 ACP 适配器
依赖 ProactorEventLoop 才能创建子进程，两者不可兼得。存储逻辑保持官方同步
PostgresSaver 不变，只把它的阻塞调用经 ``asyncio.to_thread`` 交给线程执行——
与项目 Store（generate_pm_store 的同步 PostgresStore + 线程执行器）同一做法。
psycopg 同步连接不能被多个线程同时使用，因此同一时刻只放行一个数据库调用：
所有 async 方法都在服务器唯一的事件循环上运行，一个实例级 asyncio.Lock 即可
保证 to_thread 的数据库操作串行（等待期间挂起，不阻塞循环）。
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)

_LOGGER = logging.getLogger(__name__)


class ThreadOffloadedPostgresSaver(BaseCheckpointSaver):
    """官方同步 PostgresSaver 的循环桥接：阻塞调用卸载到线程，串行放行。

    只覆盖运行时适配层要求的 async 面（aput/aput_writes/aget_tuple/aget/
    alist/adelete_thread）；其余属性经 __getattr__ 透传给内层 saver。
    """

    def __init__(self, inner):
        """Wrap an already-connected official PostgresSaver."""
        super().__init__(serde=inner.serde)
        self._inner = inner
        self._lock = asyncio.Lock()

    def get_next_version(self, current, channel):
        """Delegate version numbering to the official saver."""
        return self._inner.get_next_version(current, channel)

    async def aget(self, config):
        """Read the latest checkpoint through the thread executor."""
        async with self._lock:
            return await asyncio.to_thread(self._inner.get, config)

    async def aget_tuple(self, config) -> CheckpointTuple | None:
        """Read the latest checkpoint tuple through the thread executor."""
        async with self._lock:
            return await asyncio.to_thread(self._inner.get_tuple, config)

    async def aput(self, config, checkpoint: Checkpoint, metadata: CheckpointMetadata, new_versions):
        """Store a checkpoint through the thread executor."""
        async with self._lock:
            return await asyncio.to_thread(
                self._inner.put, config, checkpoint, metadata, new_versions
            )

    async def aput_writes(self, config, writes, task_id, task_path=""):
        """Store intermediate writes through the thread executor."""
        async with self._lock:
            return await asyncio.to_thread(
                self._inner.put_writes, config, writes, task_id, task_path
            )

    async def alist(self, config, *, filter=None, before=None, limit=None):
        """List checkpoints through the thread executor, then stream the page."""
        async with self._lock:
            items = await asyncio.to_thread(
                lambda: list(self._inner.list(config, filter=filter, before=before, limit=limit))
            )
        for item in items:
            yield item

    async def adelete_thread(self, thread_id: str) -> None:
        """Delete one thread's checkpoints through the thread executor."""
        async with self._lock:
            return await asyncio.to_thread(self._inner.delete_thread, thread_id)

    def __getattr__(self, name):
        """Everything else (config, serde access…) comes from the inner saver."""
        return getattr(self._inner, name)


@asynccontextmanager
async def generate_pm_checkpointer():
    """在服务器生命周期内打开官方 PostgresSaver（官方文档示例形状）。"""
    uri = os.getenv("LANGGRAPH_POSTGRES_URI")
    if not uri:
        # 与项目 Store 相同的回退：未配置数据库时交还运行时默认（内存）。
        # 提示要显眼：这条回退意味着会话检查点重启即丢，HITL 恢复不可用。
        _LOGGER.warning(
            "LANGGRAPH_POSTGRES_URI is not set; PM Store and session checkpoints "
            "fall back to in-memory storage and are lost on restart (ephemeral)."
        )
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(uri) as saver:
        await asyncio.to_thread(saver.setup)
        yield ThreadOffloadedPostgresSaver(saver)
