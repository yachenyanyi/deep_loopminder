import asyncio
import sys
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

# Windows系统需要设置兼容的事件循环策略
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

async def test_postgres_connection():
    try:
        # 使用正确的连接字符串 - 更新为你的PostgreSQL配置
        DB_URI = 'postgresql://postgres:11226647jqk@localhost:5432/postgres?sslmode=disable'
        async with AsyncPostgresSaver.from_conn_string(DB_URI) as checkpointer:
            await checkpointer.setup()
            print('✅ PostgreSQL 连接成功')
    except Exception as e:
        print(f'❌ PostgreSQL 连接失败: {e}')

if __name__ == "__main__":
    asyncio.run(test_postgres_connection())