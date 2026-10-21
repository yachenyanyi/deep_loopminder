"""PM 存储入口：优先复用官方运行时，独立命令沿用项目数据库提供方。"""

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

from langgraph.config import get_store
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore


@dataclass(frozen=True, slots=True)
class PMMemoryStore:
    """存储对象及其类型、持久化能力，供 PM 记忆判断当前运行环境。"""

    store: BaseStore
    backend: str
    durability: str


async def get_pm_memory_store() -> PMMemoryStore:
    """复用官方运行时或项目已有存储，不额外创建另一套数据层。"""
    # 官方 Server 已提供 Store 时直接复用，避免图工厂再创建另一份连接。
    try:
        store = get_store()
    except RuntimeError:
        store = None
    if store is None:
        # 普通 Python 调用没有图上下文，继续复用项目原有的数据库提供方。
        from src.deep_agents.db import get_postgres_store
        store = await get_postgres_store()
    if isinstance(store, InMemoryStore):
        return PMMemoryStore(store=store, backend="memory", durability="ephemeral")
    return PMMemoryStore(
        store=store,
        backend=type(store).__name__,
        durability="durable",
    )


@asynccontextmanager
async def generate_pm_store():
    """通过官方自定义 Store 生命周期，为 Server 和所有图提供同一个存储。"""
    uri = os.getenv("LANGGRAPH_POSTGRES_URI")
    if not uri:
        yield InMemoryStore()
        return
    # 官方同步 PostgresStore 也实现异步 BaseStore 接口，通过其线程执行器兼容
    # Windows Server 的 Proactor 循环；不自行实现连接池或跨循环转发。
    from langgraph.store.postgres import PostgresStore
    with PostgresStore.from_conn_string(uri) as store:
        store.setup()
        yield store
