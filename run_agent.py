#!/usr/bin/env python3
"""
Chat Agent CLI 入口

简单的对话测试工具，使用 chat_agent（前台接待）。
支持与其他代理（高级工程师、情报分析师等）协作。
"""

import asyncio
import sys
import os
import argparse
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
sys.path.insert(0, BASE_DIR)
from src.deep_agents.agents.intelligent_local import create_intelligent_deep_agent
from src.deep_agents.agents.collaborative_agents import create_chat_agent
from src.middlewares.agent.thread_config import get_thread_config_manager
from langgraph.types import Command


async def run_one_shot(agent, thread_id, user_input):
    """运行单次对话"""
    messages = [{"role": "user", "content": user_input}]
    print(f"\nUser: {user_input}")
    
    # 第一次运行
    async for event in agent.astream_events(
        {"messages": messages},
        version="v2",
        config={"configurable": {"thread_id": thread_id}}
    ):
        if event["event"] == "on_chat_model_stream":
            chunk = event["data"]["chunk"]
            if hasattr(chunk, "content") and chunk.content:
                print(chunk.content, end="", flush=True)

    # 检查是否中断
    state = await agent.aget_state({"configurable": {"thread_id": thread_id}})
    while state.next:
        # 处理中断
        interrupt_msg = ""
        for task in state.tasks:
            if task.interrupts:
                interrupt_msg = task.interrupts[0].value.get("message", "需要审批")
                break
        
        if not interrupt_msg:
            break
            
        print(f"\n\n🛑 [审批请求]: {interrupt_msg}")
        choice = input("是否批准? (y/n/edit): ").strip().lower()
        
        if choice == 'y':
            resume_val = "approve"
        elif choice == 'n':
            resume_val = "reject"
        elif choice == 'edit':
            new_args_str = input("输入修改后的参数 (JSON): ").strip()
            import json
            try:
                resume_val = {"type": "edit", "edited_args": json.loads(new_args_str)}
            except Exception as e:
                print(f"❌ JSON 解析失败: {e}")
                break
        else:
            print("❌ 无效输入，已取消操作")
            break

        # 恢复执行
        async for event in agent.astream_events(
            Command(resume=resume_val),
            version="v2",
            config={"configurable": {"thread_id": thread_id}}
        ):
            if event["event"] == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                if hasattr(chunk, "content") and chunk.content:
                    print(chunk.content, end="", flush=True)
        
        state = await agent.aget_state({"configurable": {"thread_id": thread_id}})
    
    print("\n")


async def main():
    parser = argparse.ArgumentParser(description="AI Agent CLI 工具")
    parser.add_argument("query", nargs="?", help="要发送给代理的消息（如果提供，则运行单次对话后退出）")
    parser.add_argument("--agent", "-a", choices=["local", "chat"], default="local", help="选择代理类型 (默认: local)")
    parser.add_argument("--thread", "-t", help="指定使用的 Thread ID")
    parser.add_argument("--new", "-n", action="store_true", help="创建新对话线程")
    parser.add_argument("--list", "-l", action="store_true", help="列出所有线程信息并退出")
    
    args = parser.parse_args()

    # 初始化线程管理器
    thread_manager = get_thread_config_manager(server_url="http://127.0.0.1:2024")
    agent_name = "intelligent_local_agent" if args.agent == "local" else "chat_agent"

    # 处理列表命令
    if args.list:
        info = thread_manager.list_all_threads()
        print("\n📋 线程信息:")
        for name, t_info in info.items():
            print(f"  {name}: {t_info.current[:8]}... (历史: {len(t_info.history)})")
        return

    print("正在初始化代理...")
    if args.agent == "local":
        agent = await create_intelligent_deep_agent()
    else:
        agent = await create_chat_agent()

    # 确定 thread_id
    if args.new:
        thread_id = await thread_manager.create_new_thread(agent_name)
        print(f"✨ 已创建新对话线程: {thread_id[:8]}...")
    elif args.thread:
        thread_id = args.thread
        print(f"� 使用指定的 Thread ID: {thread_id[:8]}...")
    else:
        thread_info = await thread_manager.get_thread(agent_name)
        thread_id = thread_info.current
        print(f"✅ 使用当前 Thread ID: {thread_id[:8]}...")

    # 单次调用模式
    if args.query:
        await run_one_shot(agent, thread_id, args.query)
        return

    # 交互模式
    print("-" * 50)
    print(f"🚀 {args.agent.upper()} Agent 已就绪！输入 'exit' 或 'q' 退出。")
    print("-" * 50)

    while True:
        try:
            user_input = input("\n你: ").strip()

            if user_input.lower() in ['exit', 'quit', 'q']:
                print("再见！")
                break

            if not user_input:
                continue

            if user_input.lower() == 'new':
                thread_id = await thread_manager.create_new_thread(agent_name)
                print(f"✅ 已创建新对话，Thread ID: {thread_id[:8]}...")
                continue

            if user_input.lower() == 'info':
                info = thread_manager.list_all_threads()
                print("\n📋 线程信息:")
                for name, t_info in info.items():
                    print(f"  {name}: {t_info.current[:8]}... (历史: {len(t_info.history)})")
                continue

            # 复用 astream_events 逻辑
            print("Agent: ", end="", flush=True)
            async for event in agent.astream_events(
                {"messages": [{"role": "user", "content": user_input}]},
                version="v2",
                config={"configurable": {"thread_id": thread_id}}
            ):
                if event["event"] == "on_chat_model_stream":
                    chunk = event["data"]["chunk"]
                    if hasattr(chunk, "content") and chunk.content:
                        print(chunk.content, end="", flush=True)
            
            # 检查是否中断并处理
            state = await agent.aget_state({"configurable": {"thread_id": thread_id}})
            while state.next:
                interrupt_msg = ""
                for task in state.tasks:
                    if task.interrupts:
                        interrupt_msg = task.interrupts[0].value.get("message", "需要审批")
                        break
                
                if not interrupt_msg:
                    break
                    
                print(f"\n\n🛑 [审批请求]: {interrupt_msg}")
                choice = input("是否批准? (y/n/edit): ").strip().lower()
                
                if choice == 'y':
                    resume_val = "approve"
                elif choice == 'n':
                    resume_val = "reject"
                elif choice == 'edit':
                    new_args_str = input("输入修改后的参数 (JSON): ").strip()
                    import json
                    try:
                        resume_val = {"type": "edit", "edited_args": json.loads(new_args_str)}
                    except Exception as e:
                        print(f"❌ JSON 解析失败: {e}")
                        break
                else:
                    print("❌ 无效输入，已取消操作")
                    break

                async for event in agent.astream_events(
                    Command(resume=resume_val),
                    version="v2",
                    config={"configurable": {"thread_id": thread_id}}
                ):
                    if event["event"] == "on_chat_model_stream":
                        chunk = event["data"]["chunk"]
                        if hasattr(chunk, "content") and chunk.content:
                            print(chunk.content, end="", flush=True)
                
                state = await agent.aget_state({"configurable": {"thread_id": thread_id}})
            
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