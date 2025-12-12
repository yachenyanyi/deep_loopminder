import asyncio
from src.history.history_manager import HistoryManager
from src.deep_agents.deep_agent import get_postgres_store
from dotenv import load_dotenv
load_dotenv()


async def demo():
    store = await get_postgres_store()  # src/deep_agents/deep_agent.py:60-64
    hm = HistoryManager(thread_id="main_chat", checkpoint_ns="main")
    hm.attach_store(store)              # 绑定Store
    print(await hm.load_latest())       # 可能是 False（首次使用）

    hm.record_user("你好")
    hm.record_ai("你好！我是你的助手")
    await hm.save_latest()              # 保存最新历史

    print(await hm.load_latest())       # 现在应为 True
    print(hm._messages)                 # 查看消息记录

asyncio.run(demo())