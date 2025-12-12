from langchain_core.messages import HumanMessage
import os

from src.tools.api_tools import call_tool_tool, list_resources_tool, cleanup_mcp_client
from src.deep_agents.deep_agent import create_role_playing_agent, cleanup_postgres

from dotenv import load_dotenv
import asyncio
import sys

load_dotenv()

async def get_input(prompt: str) -> str:
    """异步获取用户输入，避免阻塞事件循环"""
    return await asyncio.to_thread(input, prompt)

async def chat_loop():
    """全异步的主循环"""
    print("智能助手已启动，输入 'quit' 或 'exit' 退出\n")
    
    # 异步创建角色扮演代理，使用PostgreSQL持久化存储
    Role_Playing_Agent = await create_role_playing_agent()
    print("角色扮演代理已创建，使用PostgreSQL持久化存储\n")
    
    messages = []  # 保存对话历史
    
    try:
        while True:
            try:
                user_input = (await get_input("你: ")).strip()
                
                # 退出条件
                if user_input.lower() in ['quit', 'exit', '退出']:
                    print("再见！")
                    break
                
                # 空输入处理
                if not user_input:
                    print("请输入内容\n")
                    continue
                
                # 添加用户消息
                messages.append(HumanMessage(content=user_input))
                
                # 调用代理 (异步流式)
                print("\n助手: ", end="", flush=True)
                
                # 使用 astream_events 进行异步流式调用
                response_content = ""
                config = {
                    "configurable": {
                        "thread_id": "main_chat", 
                        "checkpoint_ns": ""
                    },
                    "recursion_limit": 50  # 增加递归限制
                }
                async for event in Role_Playing_Agent.astream_events(
                    {"messages": messages},
                    config=config,
                    version="v1"
                ):
                    kind = event["event"]
                    
                    if kind == "on_chat_model_stream":
                        content = event["data"]["chunk"].content
                        if content:
                            print(content, end="", flush=True)
                            response_content += content
                            
                # 构造完整的助手消息并添加到历史
                from langchain_core.messages import AIMessage
                assistant_message = AIMessage(content=response_content)
                messages.append(assistant_message)
                
                print("\n")  # 响应完成后换行
                
            except KeyboardInterrupt:
                print("\n\n程序被中断，正在退出...")
                break
            except Exception as e:
                print(f"\n发生错误: {e}")
                print("请重试或输入 'quit' 退出\n")
                
    finally:
        # 清理资源
        print("正在清理资源...")
        await cleanup_mcp_client()
        await cleanup_postgres()

# 主函数
async def main():
    """主函数"""
    try:
        await chat_loop()
    except Exception as e:
        print(f"程序运行错误: {e}")
        sys.exit(1)

if __name__ == "__main__":
    # 修复Windows上的事件循环问题
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(main())