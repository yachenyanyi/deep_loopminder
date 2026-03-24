#!/usr/bin/env python3
"""
简单的代理导入测试

测试所有代理是否能正确导入和创建。
"""

import asyncio
import sys
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE_DIR, ".env"))


async def test_model_loading():
    """测试模型懒加载"""
    print("=" * 50)
    print("1. 测试模型懒加载")
    print("=" * 50)

    from src.models.llm import get_default_model, list_available_models, print_model_status

    print("\n可用模型状态:")
    print_model_status()

    print("\n获取默认模型...")
    model = get_default_model()
    print(f"✅ 默认模型: {type(model).__name__}")


async def test_employee_registry():
    """测试员工注册表"""
    print("\n" + "=" * 50)
    print("2. 测试员工注册表")
    print("=" * 50)

    from src.deep_agents.agents.employee_registry import (
        COLLABORATIVE_EMPLOYEES,
        get_employee_by_name,
        get_all_employee_names,
    )

    print(f"\n员工数量: {len(COLLABORATIVE_EMPLOYEES)}")
    for emp in COLLABORATIVE_EMPLOYEES:
        print(f"  - {emp['name']}: {emp['role']}")

    print(f"\n按名称查询 coder_agent: {get_employee_by_name('coder_agent')['description']}")
    print(f"所有名称: {get_all_employee_names()}")


async def test_middleware():
    """测试通信中间件"""
    print("\n" + "=" * 50)
    print("3. 测试通信中间件")
    print("=" * 50)

    from src.deep_agents.agents.collaborative_agents import create_communication_middleware

    middleware = create_communication_middleware("chat_agent")
    tools = middleware.tools()
    print(f"\n中间件提供的工具: {[t.name for t in tools]}")
    print(f"工具数量: {len(tools)}")


async def test_agent_creation():
    """测试代理创建函数导入"""
    print("\n" + "=" * 50)
    print("4. 测试代理创建函数导入")
    print("=" * 50)

    from src.deep_agents.agents.collaborative_agents import (
        create_chat_agent,
        create_coordinator_agent,
        create_coder_agent,
        create_researcher_agent,
        create_assistant_agent,
    )

    print("\n✅ 所有创建函数导入成功:")
    print("  - create_chat_agent")
    print("  - create_coordinator_agent")
    print("  - create_coder_agent")
    print("  - create_researcher_agent")
    print("  - create_assistant_agent")


async def test_existing_agents():
    """测试现有代理"""
    print("\n" + "=" * 50)
    print("5. 测试现有代理")
    print("=" * 50)

    from src.agents.agent import tools_Assistant, Intelligent_Assistant

    print(f"\ntools_Assistant: {type(tools_Assistant).__name__}")
    print(f"Intelligent_Assistant: {type(Intelligent_Assistant).__name__}")
    print("✅ 现有代理导入成功")


async def test_full_agent_creation():
    """测试完整代理创建（需要 PostgreSQL）"""
    print("\n" + "=" * 50)
    print("6. 测试完整代理创建（需要 PostgreSQL）")
    print("=" * 50)

    try:
        from src.deep_agents.agents.collaborative_agents import create_chat_agent

        print("\n正在创建 chat_agent...")
        agent = await create_chat_agent()
        print(f"✅ chat_agent 创建成功: {type(agent).__name__}")
    except Exception as e:
        print(f"⚠️ 创建失败: {e}")
        print("   (可能是 PostgreSQL 未启动或连接失败)")


async def main():
    """主函数"""
    print("\n🚀 开始测试代理导入...\n")

    try:
        await test_model_loading()
        await test_employee_registry()
        await test_middleware()
        await test_agent_creation()
        await test_existing_agents()
        await test_full_agent_creation()

        print("\n" + "=" * 50)
        print("✅ 所有测试完成!")
        print("=" * 50)

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(main())