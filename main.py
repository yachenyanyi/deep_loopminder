from langchain_core.messages import HumanMessage
import os

os.environ["DEEPSEEK_API_KEY"] = "sk-39399bdd68a34751a014e77f93d17f31"

from src.tools.api_tools import call_tool_tool, list_resources_tool, cleanup_mcp_client

from src.deep_agents.deep_agent import Intelligent_Deep_Assistant


import asyncio
import sys

# 创建智能代理


async def get_input(prompt: str) -> str:
    """异步获取用户输入，避免阻塞事件循环"""
    return await asyncio.to_thread(input, prompt)

async def chat_loop():
    """全异步的主循环"""
    print("智能助手已启动，输入 'quit' 或 'exit' 退出\n")
    
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
                async for event in Intelligent_Deep_Assistant.astream_events(
                    {"messages": messages},
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
                
                print("\n")
                
            except asyncio.CancelledError:
                raise
            except KeyboardInterrupt:
                print("\n\n再见！")
                break
            except Exception as e:
                print(f"\n错误: {e}\n")
                continue
                
    finally:
        # 程序退出时清理资源
        print("正在清理资源...")
        await cleanup_mcp_client()
        print("资源清理完成")

if __name__ == "__main__":
    # Windows 上可能需要设置 ProactorEventLoop（Python 3.8+ 默认已经是了）
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    try:
        asyncio.run(chat_loop())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"程序运行出错: {e}")
