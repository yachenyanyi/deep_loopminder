#!/usr/bin/env python3
"""
Deep Agent 使用示例

本示例展示了如何使用不同类型的 deep agent 处理各种任务场景。
"""

import asyncio
from src.deep_agents.deep_agent import get_agent_by_use_case, list_all_agents


async def example_basic_filesystem():
    """基础文件系统代理示例"""
    print("=== 基础文件系统代理示例 ===")
    agent = get_agent_by_use_case("basic_filesystem")
    
    # 示例：创建和编辑文件
    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": """
        请在 workspace 目录下创建一个名为 "project_plan.md" 的文件，
        包含以下内容：
        - 项目概述
        - 技术栈选择
        - 开发计划
        - 风险评估
        """}]
    })
    print(f"基础文件系统代理结果: {result}")


async def example_analytics():
    """数据分析代理示例"""
    print("=== 数据分析代理示例 ===")
    agent = get_agent_by_use_case("analytics")
    
    # 示例：分析CSV数据
    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": """
        创建一个包含以下数据的CSV文件 sales_data.csv：
        日期,产品,销量,收入
        2024-01-01,产品A,100,5000
        2024-01-02,产品B,150,7500
        2024-01-03,产品A,120,6000
        
        然后分析这个数据集，计算总销量、平均收入、
        最畅销产品，并生成简单的统计报告。
        """}]
    })
    print(f"数据分析代理结果: {result}")


async def example_persistent_memory():
    """持久化记忆代理示例"""
    print("=== 持久化记忆代理示例 ===")
    agent = get_agent_by_use_case("persistent_memory")
    
    # 示例：创建知识库
    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": """
        创建一个长期记忆文件 /memories/tech_stack_preferences.md，
        记录我偏好的技术栈：
        - 后端：Python FastAPI
        - 前端：React + TypeScript  
        - 数据库：PostgreSQL
        - 部署：Docker + Kubernetes
        
        这个信息需要在后续会话中保持可用。
        """}]
    })
    print(f"持久化记忆代理结果: {result}")


async def example_hybrid_storage():
    """混合存储代理示例"""
    print("=== 混合存储代理示例 ===")
    agent = get_agent_by_use_case("hybrid_storage")
    
    # 示例：混合存储使用
    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": """
        演示混合存储的使用：
        1. 在 /tmp/ 下创建临时工作文件 temp_analysis.py
        2. 在 /memories/ 下创建长期记忆文件 important_notes.md
        3. 在 /workspace/ 下创建项目文件 project_config.yaml
        
        解释每种存储的用途和生命周期。
        """}]
    })
    print(f"混合存储代理结果: {result}")


async def example_enterprise():
    """企业级代理示例"""
    print("=== 企业级代理示例 ===")
    agent = get_agent_by_use_case("enterprise")
    
    # 示例：企业文档管理
    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": """
        作为企业级AI助手，请：
        1. 在 /documents/ 目录创建项目提案文档
        2. 在 /audit/ 目录记录操作日志
        3. 在 /config/ 目录创建配置文件
        
        确保所有操作符合企业安全和合规要求。
        """}]
    })
    print(f"企业级代理结果: {result}")


def show_available_agents():
    """显示所有可用代理"""
    print("=== 可用代理类型 ===")
    agents = list_all_agents()
    for name, description in agents.items():
        print(f"• {name}: {description}")
    print()


def show_usage_examples():
    """显示使用示例"""
    print("=== 使用示例 ===")
    print("""
# 获取特定类型的代理
agent = get_agent_by_use_case("basic_filesystem")

# 使用代理处理任务
result = await agent.ainvoke({
    "messages": [{"role": "user", "content": "你的任务描述"}]
})

# 可用代理类型:
# - basic_filesystem: 基础文件系统操作
# - state_only: 临时状态存储  
# - persistent_memory: 持久化记忆
# - hybrid_storage: 混合存储
# - analytics: 数据分析
# - enterprise: 企业级应用
# - intelligent_deep: 智能深度助手（默认）
    """)


async def main():
    """主函数 - 运行所有示例"""
    print("Deep Agent 使用示例")
    print("=" * 50)
    
    # 显示可用代理
    show_available_agents()
    show_usage_examples()
    
    # 运行示例（可以根据需要选择运行）
    try:
        await example_basic_filesystem()
        await example_analytics()
        await example_persistent_memory()
        await example_hybrid_storage()
        await example_enterprise()
    except Exception as e:
        print(f"示例运行出错: {e}")
        print("这可能是因为某些依赖未安装或配置不正确")


if __name__ == "__main__":
    # 运行异步主函数
    asyncio.run(main())