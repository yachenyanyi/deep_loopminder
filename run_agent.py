#!/usr/bin/env python3
"""
Chat Agent CLI 入口

简单的对话测试工具，使用 chat_agent（前台接待）。
支持与其他代理（高级工程师、情报分析师等）协作。
"""

import asyncio
import sys
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
sys.path.insert(0, BASE_DIR)

from src.deep_agents.agents.collaborative_agents import create_chat_agent
from src.middlewares.thread_config import get_thread_config_manager


async def main():
    print("=" * 50)
    print("  Chat Agent - AI 公司前台接待")
    print("=" * 50)
    print()
    print("正在初始化...")

    # 创建 chat_agent
    agent = await create_chat_agent()

    # 获取或创建 thread_id
    thread_manager = get_thread_config_manager(server_url="http://127.0.0.1:2024")
    thread_info = await thread_manager.get_thread("chat_agent")
    thread_id = thread_info.current

    print(f"✅ Chat Agent 已就绪！")
    print(f"📝 Thread ID: {thread_id[:8]}...")
    print()
    print("命令:")
    print("  - 直接输入消息进行对话")
    print("  - 'exit' 或 'q' 退出")
    print("  - 'new' 开始新对话")
    print("  - 'info' 查看线程信息")
    print("-" * 50)

    while True:
        try:
            user_input = input("\n你: ").strip()

            # 退出命令
            if user_input.lower() in ['exit', 'quit', 'q']:
                print("再见！")
                break

            # 空输入
            if not user_input:
                continue

            # 新对话命令
            if user_input.lower() == 'new':
                new_thread_id = await thread_manager.create_new_thread("chat_agent")
                thread_id = new_thread_id
                print(f"✅ 已创建新对话，Thread ID: {thread_id[:8]}...")
                continue

            # 查看线程信息
            if user_input.lower() == 'info':
                info = thread_manager.list_all_threads()
                print("\n📋 线程信息:")
                for name, t_info in info.items():
                    print(f"  {name}: {t_info.current[:8]}... (历史: {len(t_info.history)})")
                continue

            # 发送消息
            messages = [{"role": "user", "content": user_input}]

            print("\nAgent: ", end="", flush=True)

            async for event in agent.astream_events(
                {"messages": messages},
                version="v2",
                config={
                    "configurable": {"thread_id": thread_id},
                    "recursion_limit": 100
                }
            ):
                if event["event"] == "on_chat_model_stream":
                    chunk = event["data"]["chunk"]
                    if hasattr(chunk, "content") and chunk.content:
                        print(chunk.content, end="", flush=True)

            print()

        except KeyboardInterrupt:
            print("\n再见！")
            break
        except Exception as e:
            print(f"\n❌ 错误: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(main())