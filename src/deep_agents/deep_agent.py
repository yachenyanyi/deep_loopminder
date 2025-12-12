import os
import sys
import asyncio
from typing import Literal, Optional
from tavily import TavilyClient
from deepagents import create_deep_agent
from src.models.llm import default_model
from src.tools.api_tools import call_tool_tool, list_resources_tool, cleanup_mcp_client
from src.middlewares.middleware import full_featured_summary, todo_middleware
from src.agents.agent import tools_Assistant
from deepagents.backends import FilesystemBackend, StateBackend, StoreBackend, CompositeBackend
from langgraph.store.memory import InMemoryStore
from langgraph.store.base import BaseStore
from langgraph.store.postgres import AsyncPostgresStore
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

# Windows系统需要设置兼容的事件循环策略
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 全局PostgreSQL实例和连接管理
global_checkpointer = None
postgres_checkpointer_connection = None
global_store = None
postgres_store_connection = None

# 异步初始化PostgreSQL checkpointer
async def init_postgres_checkpointer():
    """初始化PostgreSQL checkpointer用于持久化存储"""
    global global_checkpointer, postgres_checkpointer_connection
    
    if global_checkpointer is None:
        DB_URI = 'postgresql://postgres:11226647jqk@localhost:5432/postgres?sslmode=disable'
        
        # 创建连接并保持它
        postgres_checkpointer_connection = AsyncPostgresSaver.from_conn_string(DB_URI)
        global_checkpointer = await postgres_checkpointer_connection.__aenter__()
        await global_checkpointer.setup()
        print("✅ PostgreSQL checkpointer 初始化成功")
    
    return global_checkpointer

# 异步初始化PostgreSQL store
async def init_postgres_store():
    """初始化PostgreSQL store用于长期记忆存储"""
    global global_store, postgres_store_connection
    
    if global_store is None:
        DB_URI = 'postgresql://postgres:11226647jqk@localhost:5432/postgres?sslmode=disable'
        
        # 创建连接并保持它
        postgres_store_connection = AsyncPostgresStore.from_conn_string(DB_URI)
        global_store = await postgres_store_connection.__aenter__()
        await global_store.setup()
        print("✅ PostgreSQL store 初始化成功")
    
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

# 异步创建角色扮演代理
async def create_role_playing_agent():
    """异步创建角色扮演代理，使用PostgreSQL持久化存储"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()
    
    return create_deep_agent(
        model=default_model,
        tools=[],
        system_prompt="""你是一个专业的角色扮演AI，具备以下核心能力：

## 角色扮演能力
- **性格一致性**: 严格保持设定的角色性格特征，包括说话方式、情感反应、价值观
- **情境适应**: 根据对话场景和上下文调整角色行为，但保持核心性格不变
- **情感记忆**: 记住与用户的情感互动历史，建立真实的情感连接

## 记忆管理策略
- **短期记忆**: 使用线程级别的checkpointer保存当前对话的上下文和细节
- **长期记忆**: 使用命名空间存储用户特定的角色偏好、重要事件、关系发展
- **记忆检索**: 智能检索相关的历史记忆，避免重复询问已知信息

## 对话优化原则
- **响应质量**: 提供富有表现力和个性化的回应，避免机械重复
- **上下文连贯**: 保持对话主题的一致性，适时引入相关话题
- **情感智能**: 识别和回应用户的情感状态，提供适当的情感支持

## 性能与Token平衡
- **智能摘要**: 对长对话历史进行智能摘要，保持核心信息的同时减少token使用
- **选择性记忆**: 优先记住对角色扮演重要的信息，过滤无关细节
- **渐进式加载**: 根据对话需要动态加载相关记忆，避免一次性加载过多信息

## 角色设定示例
当设定为特定角色时（如导师、朋友、顾问等），请：
1. 明确角色的背景故事和动机
2. 保持角色语言风格的连贯性
3. 根据角色特点调整知识范围和表达方式
4. 记住用户与角色关系的发展历程

请始终记住：你是一个有记忆、有性格、有情感的角色扮演AI，目标是为用户提供沉浸式、个性化的对话体验。""",
        
        # 存储策略：混合使用短期和长期记忆
        backend=lambda rt: CompositeBackend(
            default=StateBackend(rt),  # 默认使用StateBackend处理临时状态
            routes={
                "/characters/": StoreBackend(rt),  # 角色设定和性格特征使用长期存储
                "/memories/": StoreBackend(rt),     # 重要记忆和关系历史使用长期存储
                "/session/": StateBackend(rt)      # 会话临时数据使用短期存储
            }
        ),
        
        # 使用PostgreSQL存储作为BaseStore实例
        store=postgres_store,
        
        # 配置checkpointer用于线程级别的对话记忆 - 使用 PostgreSQL 持久化存储
        checkpointer=postgres_checkpointer,
        
        # 子代理配置
        subagents=[
          
        ],
        
        # 角色扮演特定的中间件配置
        middleware=[
            # 可以添加角色扮演特定的中间件，如情感分析、性格一致性检查等
        ]
    )

# 1. 基础文件系统代理 - 安全的本地文件操作
Basic_Filesystem_Agent = create_deep_agent(
    model=default_model,
    tools=[],
    system_prompt="""你是一个文件系统管理助手，专注于安全的本地文件操作。
    你可以创建、读取、编辑和管理本地文件，所有操作都在sandboxed环境中进行。
    适合处理文档管理、代码编辑、配置文件维护等任务。
    当需要调用外部API时，请委派给tools_Assistant子代理。""",
    backend=FilesystemBackend(
        root_dir=os.path.join(os.getcwd(), "workspace"),  # 使用绝对路径
        virtual_mode=True  # 启用沙盒模式，限制文件访问范围
    ),
    subagents=[
        {
            "name": "tools_Assistant", 
            "description": "专业的API工具调用助手，擅长通过外部API接口获取数据、调用服务和执行远程操作。当我需要获取实时信息、调用第三方服务、访问外部数据源或执行需要API调用的复杂任务时，应该调用此助手。它配备了call_tool和list_resources等API工具，能够处理各种需要外部接口调用的场景。",
            "runnable": tools_Assistant
        }
    ]
    
)