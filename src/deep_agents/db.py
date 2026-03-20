import asyncio
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres import AsyncPostgresStore

# 全局PostgreSQL实例和连接管理
global_checkpointer = None
postgres_checkpointer_connection = None
global_store = None
postgres_store_connection = None
postgres_checkpointer_lock = asyncio.Lock()
postgres_store_lock = asyncio.Lock()

# 异步初始化PostgreSQL checkpointer
async def init_postgres_checkpointer():
    """初始化PostgreSQL checkpointer用于持久化存储"""
    global global_checkpointer, postgres_checkpointer_connection

    if global_checkpointer is not None:
        return global_checkpointer

    async with postgres_checkpointer_lock:
        if global_checkpointer is not None:
            return global_checkpointer
        DB_URI = 'postgresql://postgres:11226647jqk@localhost:5432/postgres?sslmode=disable'
        postgres_checkpointer_connection = AsyncPostgresSaver.from_conn_string(DB_URI)
        global_checkpointer = await postgres_checkpointer_connection.__aenter__()
        await global_checkpointer.setup()
        print("✅ PostgreSQL checkpointer 初始化成功")
        return global_checkpointer

# 异步初始化PostgreSQL store
async def init_postgres_store():
    """初始化PostgreSQL store用于长期记忆存储"""
    global global_store, postgres_store_connection

    if global_store is not None:
        return global_store

    async with postgres_store_lock:
        if global_store is not None:
            return global_store
        DB_URI = 'postgresql://postgres:11226647jqk@localhost:5432/postgres?sslmode=disable'
        postgres_store_connection = AsyncPostgresStore.from_conn_string(DB_URI)
        global_store = await postgres_store_connection.__aenter__()
        await global_store.setup()
        print("✅ PostgreSQL store 初始化成功")
        return global_store

async def get_postgres_store():
    global global_store
    if global_store is None:
        await init_postgres_store()
    return global_store

# 清理函数
async def cleanup_postgres():
    """清理PostgreSQL连接"""
    global postgres_checkpointer_connection, postgres_store_connection

    if postgres_checkpointer_connection:
        await postgres_checkpointer_connection.__aexit__(None, None, None)
        print("✅ PostgreSQL checkpointer 连接已清理")

    if postgres_store_connection:
        await postgres_store_connection.__aexit__(None, None, None)
        print("✅ PostgreSQL store 连接已清理")