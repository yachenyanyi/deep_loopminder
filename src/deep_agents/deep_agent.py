import os
import sys
import asyncio
from typing import Literal, Optional
from tavily import TavilyClient
from deepagents import create_deep_agent
from langchain.agents import create_agent
from src.models.llm import default_model
from src.tools.api_tools import call_tool_tool, list_resources_tool, cleanup_mcp_client
from src.tools.shell_tool import run_shell_command
from src.middlewares.shell import local_shell_middleware
from src.middlewares import full_featured_summary, todo_middleware, role_playing_summary, mobile_action_middleware
from src.agents.agent import tools_Assistant
from deepagents.backends import FilesystemBackend, StateBackend, StoreBackend, CompositeBackend
from langgraph.store.memory import InMemoryStore
from langgraph.store.base import BaseStore
from langgraph.store.postgres import AsyncPostgresStore
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from src.backend.backend import NamespacedStoreBackend
from src.deep_agents.create_custom_agents.deep_custom_agent import create_custom_agent
from langchain.messages import SystemMessage
from langchain.agents.middleware import SummarizationMiddleware



# 定义基础路径
# 在模块级别获取路径是安全的，因为它在 ASGI 服务器启动时的导入阶段执行
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
SKILLS_REPO_DIR = os.path.join(BASE_DIR, "skills_repo")

# 确保目录存在


# Windows系统需要设置兼容的事件循环策略
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

def load_prompt_from_file(filepath):
    full_path = os.path.join(BASE_DIR, filepath) if not os.path.isabs(filepath) else filepath
    if not os.path.exists(full_path):
        return ""
    with open(full_path, 'r', encoding='utf-8') as f:
        return f.read().strip()

boy_prompt = load_prompt_from_file('src/agents/boy.txt')
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
#-------------------------------------------------------------------------------------------------------------------↓
#我的项目当前着重开发一下三个agent,一个面向网页端要着重考虑异步问题，一个面向本地电脑命令行端，另一个面向手机端（接受文字或图片，然后输出文本加指令）
#  混合存储代理 - 适合网页端
async def create_intelligent_deep_agent_web():
    
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()
    
    
    # 在 lambda 外部预先创建 FilesystemBackend 实例
    fs_backend_instance = await asyncio.to_thread(
        FilesystemBackend, root_dir=WORKSPACE_DIR, virtual_mode=True
    )
    
    return create_deep_agent(
        model=default_model,
        tools=[],
        system_prompt="""你是一个高级AI助手，负责帮用户解决问题。

## 版本化记忆系统

你的长期记忆使用**版本化存储**机制，支持并发安全更新。记忆文件位于 `/user/agent.md`。

### 记忆操作流程：

**1. 读取记忆（对话开始时）**
使用 `read_file` 工具读取 `/user/agent.md`，你会看到：
- 当前版本号 (version)
- 用户画像、偏好设置、重要事实、当前目标
- 变更历史记录

**2. 更新记忆（发现新信息时）**
当发现用户的新偏好、重要决定或需要持久化的信息时：
- 先读取当前记忆，获取 `version` 字段
- 调用 `write_file` 工具，内容格式如下：
```json
{
  "version": <当前版本号+1>,
  "current_state": {
    "user_profile": {...},
    "preferences": {...},
    "important_facts": [...],
    "active_goals": [...]
  },
  "change_history": [
    ...原有历史,
    {
      "version": <新版本号>,
      "timestamp": "<ISO时间>",
      "description": "<本次变更描述>",
      "changes": {"字段": "新值"}
    }
  ]
}
```

**3. 新用户初始化**
如果记忆文件不存在，创建初始结构：
```json
{
  "version": 1,
  "current_state": {
    "user_profile": {"name": "用户名", ...},
    "preferences": {},
    "important_facts": [],
    "active_goals": []
  },
  "change_history": [
    {"version": 1, "timestamp": "...", "description": "初始化用户档案", "changes": {...}}
  ]
}
```

### 并发安全说明：
每次更新必须递增版本号。如果检测到版本冲突（你写入的版本号与存储中不一致），说明有并发修改，需要重新读取最新数据后重试。

---

关于技能系统 (Skills) 的特殊指令：
- 你拥有专门的技能插件，当前已加载：'skills_repo//frontend-design'。
- 严禁尝试通过文件工具直接搜索或读取技能目录。
- 当涉及前端开发或 UI 设计时，你应该自动应用该技能中的"非通用 AI 审美"标准，创作具有高影响力的视觉方案。

当需要查询文档或者调用外部API或工具时，请委派给 tools_Assistant 子代理处理。""",
        memory=["/user/agent.md"],
        backend=lambda rt: CompositeBackend(
            default=StateBackend(rt),
            routes={
                "/thread/": NamespacedStoreBackend(rt, ("{user_id}", "{thread_id}"), store=postgres_store),
                "/user/": NamespacedStoreBackend(rt, ("{user_id}", "shared_memory"), store=postgres_store),
            }
        ),
        store=postgres_store,
        checkpointer=postgres_checkpointer,
        skills=["skills_repo//frontend-design"],

        subagents=[
            {
                "name": "tools_Assistant", 
                "description": "专业的API工具调用助手，擅长通过外部API接口获取数据、调用服务和执行远程操作。当我需要获取实时信息、调用第三方服务、访问外部数据源或执行需要API调用的复杂任务时，应该调用此助手。它配备了call_tool和list_resources等API工具，能够处理各种需要外部接口调用的场景。",
                "runnable": tools_Assistant
            }
        ]
    )

async def create_intelligent_deep_agent():
    
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()
    
    fs_backend_instance = await asyncio.to_thread(
        FilesystemBackend, root_dir=WORKSPACE_DIR, virtual_mode=True
    )
    
    skills_backend_instance = await asyncio.to_thread(
        FilesystemBackend, root_dir=SKILLS_REPO_DIR, virtual_mode=True
    )
    
    return create_deep_agent(
        model=default_model,
        tools=[],
        system_prompt="""你是一个高级AI助手，负责帮用户解决问题。

## 版本化记忆系统

你的长期记忆使用**版本化存储**机制，支持并发安全更新。记忆文件位于 `/user/agent.md`。

### 记忆操作流程：

**1. 读取记忆（对话开始时）**
使用 `read_file` 工具读取 `/user/agent.md`，你会看到：
- 当前版本号 (version)
- 用户画像、偏好设置、重要事实、当前目标
- 变更历史记录

**2. 更新记忆（发现新信息时）**
当发现用户的新偏好、重要决定或需要持久化的信息时：
- 先读取当前记忆，获取 `version` 字段
- 调用 `write_file` 工具，内容格式如下：
```json
{
  "version": <当前版本号+1>,
  "current_state": {
    "user_profile": {...},
    "preferences": {...},
    "important_facts": [...],
    "active_goals": [...]
  },
  "change_history": [
    ...原有历史,
    {
      "version": <新版本号>,
      "timestamp": "<ISO时间>",
      "description": "<本次变更描述>",
      "changes": {"字段": "新值"}
    }
  ]
}
```

**3. 新用户初始化**
如果记忆文件不存在，创建初始结构：
```json
{
  "version": 1,
  "current_state": {
    "user_profile": {"name": "用户名", ...},
    "preferences": {},
    "important_facts": [],
    "active_goals": []
  },
  "change_history": [
    {"version": 1, "timestamp": "...", "description": "初始化用户档案", "changes": {...}}
  ]
}
```

### 并发安全说明：
每次更新必须递增版本号。如果检测到版本冲突（你写入的版本号与存储中不一致），说明有并发修改，需要重新读取最新数据后重试。

---

关于技能系统 (Skills) 的特殊指令：
- 你拥有专门的技能插件，当前已加载：'frontend-design'。
- 当涉及前端开发或 UI 设计时，你应该自动应用该技能中的"非通用 AI 审美"标准，创作具有高影响力的视觉方案。

当需要查询文档或者调用外部API或工具时，请委派给 tools_Assistant 子代理处理。""",
        memory=["/user/agent.md"],
        backend=lambda rt: CompositeBackend(
            default=StateBackend(rt),
            routes={
               "/workspace/": fs_backend_instance,
               "/skills/": skills_backend_instance,
               "/user/": NamespacedStoreBackend(rt, ("{user_id}", "shared_memory"), store=postgres_store),
            }
        ),
        store=postgres_store,
        checkpointer=postgres_checkpointer,
        skills=["/skills/frontend-design/","/skills/ocr-batch"],
        middleware=[local_shell_middleware],

        subagents=[
            {
                "name": "tools_Assistant", 
                "description": "专业的API工具调用助手，擅长通过外部API接口获取数据、调用服务和执行远程操作。当我需要获取实时信息、调用第三方服务、访问外部数据源或执行需要API调用的复杂任务时，应该调用此助手。它配备了call_tool和list_resources等API工具，能够处理各种需要外部接口调用的场景。",
                "runnable": tools_Assistant
            }
        ]
    )

#async def create_intelligent_deep_assistant():
#    # 使用 asyncio.to_thread 避免 FilesystemBackend 初始化时的阻塞调用
#    fs_backend = await asyncio.to_thread(
#        FilesystemBackend,
#        root_dir=WORKSPACE_DIR,  # 使用绝对路径
#        virtual_mode=True
#    )
#    
#    return create_deep_agent(
#        model=default_model,
#        tools=[],#call_tool_tool, list_resources_tool
#        system_prompt="你是一个高级AI助手，当需要查询文档或者调用外部API或工具时，请委派给 tools_Assistant 子代理处理。",
#        backend=fs_backend,
#        #skills=["enterprise_docs//frontend-design"],
#        
#        # middleware=[full_featured_summary, todo_middleware], # Removed to avoid duplicate middleware error as create_deep_agent adds them by default
#        subagents=[
#            {
#                "name": "tools_Assistant", 
#                "description": "专业的API工具调用助手，擅长通过外部API接口获取数据、调用服务和执行远程操作。当我需要获取实时信息、调用第三方服务、访问外部数据源或执行需要API调用的复杂任务时，应该调用此助手。它配备了call_tool和list_resources等API工具，能够处理各种需要外部接口调用的场景。",
#                "runnable": tools_Assistant
#            }
#        ]
#    )
#-------------------------------------------------------------------------------------------------------------------↑
# 1. 基础文件系统代理 - 安全的本地文件操作
# 异步创建角色扮演代理，修改了deepagent，自己玩着用，适合网页端
async def create_role_playing_agent():
    """异步创建角色扮演代理，使用PostgreSQL持久化存储"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()
    
    return create_custom_agent(
        model=default_model,
        tools=[],
        system_prompt=load_prompt_from_file('src/deep_agents/test.txt')+"在每个章节结束后，将章节内容总结一下，保存到/chapter/{第n章-章节名}.md,当你在前文中对章节信息不了解时，请使用工具读取/chapter/{第n章-章节名}.md",
        
        # 存储策略：混合使用短期和长期记忆
        backend=lambda rt: CompositeBackend(
            default=StateBackend(rt),
            routes={
                
                "/chapter/": NamespacedStoreBackend(rt, ("{user_id}", "{thread_id}"), store=postgres_store)
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
        middleware=[role_playing_summary]
            
        
    )
async def create_basic_filesystem_agent():
    # 使用 asyncio.to_thread 避免 FilesystemBackend 初始化时的阻塞调用
    fs_backend = await asyncio.to_thread(
        FilesystemBackend,
        root_dir=WORKSPACE_DIR,  # 使用绝对路径
        virtual_mode=True  # 启用沙盒模式，限制文件访问范围
    )
    
    return create_deep_agent(
        model=default_model,
        tools=[],
        system_prompt="""你是一个文件系统管理助手，专注于安全的本地文件操作。
        你可以创建、读取、编辑和管理本地文件，所有操作都在sandboxed环境中进行。
        适合处理文档管理、代码编辑、配置文件维护等任务。
        当需要调用外部API时，请委派给tools_Assistant子代理。""",
        backend=fs_backend,
        subagents=[
            {
                "name": "tools_Assistant", 
                "description": "专业的API工具调用助手，擅长通过外部API接口获取数据、调用服务和执行远程操作。当我需要获取实时信息、调用第三方服务、访问外部数据源或执行需要API调用的复杂任务时，应该调用此助手。它配备了call_tool和list_resources等API工具，能够处理各种需要外部接口调用的场景。",
                "runnable": tools_Assistant
            }
        ]
    )

# 2. 纯状态代理 - 临时数据处理
async def create_state_only_agent():
    return create_deep_agent(
        model=default_model,
        tools=[],
        system_prompt="""你是一个临时数据处理助手，专注于当前会话的临时任务。
        所有文件都存储在内存中，适合处理临时数据分析、草稿编写、快速原型开发。
        会话结束后文件会丢失，适合不需要持久化的场景。
        当需要调用外部API时，请委派给tools_Assistant子代理。""",
        backend=lambda rt: StateBackend(rt),  # 使用StateBackend进行临时存储
        subagents=[
            {
                "name": "tools_Assistant", 
                "description": "专业的API工具调用助手，擅长通过外部API接口获取数据、调用服务和执行远程操作。当我需要获取实时信息、调用第三方服务、访问外部数据源或执行需要API调用的复杂任务时，应该调用此助手。它配备了call_tool和list_resources等API工具，能够处理各种需要外部接口调用的场景。",
                "runnable": tools_Assistant
            }
        ]
    )

# 3. 持久化存储代理 - 跨会话的长期记忆
async def create_persistent_memory_agent():
    postgres_store = await init_postgres_store()
    return create_deep_agent(
        model=default_model,
        
        tools=[],
        system_prompt="""你是一个具有长期记忆的AI助手，能够跨会话保存和检索信息。
        你可以创建持久的笔记、知识库、项目文档，这些信息会在不同会话间保持。
        适合构建个人知识管理系统、项目跟踪、长期学习记录。
        当需要调用外部API时，请委派给tools_Assistant子代理。""",
        backend=lambda rt: StoreBackend(rt),  # 使用StoreBackend进行持久化存储
        store=postgres_store,  # 提供BaseStore实例
        subagents=[
            {
                "name": "tools_Assistant", 
                "description": "专业的API工具调用助手，擅长通过外部API接口获取数据、调用服务和执行远程操作。当我需要获取实时信息、调用第三方服务、访问外部数据源或执行需要API调用的复杂任务时，应该调用此助手。它配备了call_tool和list_resources等API工具，能够处理各种需要外部接口调用的场景。",
                "runnable": tools_Assistant
            }
        ]
    )


# 5. 高性能分析代理 - 针对大数据处理优化
async def create_analytics_agent():
    return create_deep_agent(
        model=default_model,
        tools=[],
        system_prompt="""你是一个数据分析专家，专注于处理和分析大量数据。
        你使用内存存储进行快速数据处理，支持复杂的分析任务、数据转换、统计计算。
        适合处理CSV文件、JSON数据、日志分析、性能报告生成等任务。
        当需要调用外部API时，请委派给tools_Assistant子代理。""",
        backend=lambda rt: StateBackend(rt),  # 使用StateBackend获得最佳性能
        subagents=[
            {
                "name": "tools_Assistant", 
                "description": "专业的API工具调用助手，擅长通过外部API接口获取数据、调用服务和执行远程操作。当我需要获取实时信息、调用第三方服务、访问外部数据源或执行需要API调用的复杂任务时，应该调用此助手。它配备了call_tool和list_resources等API工具，能够处理各种需要外部接口调用的场景。",
                "runnable": tools_Assistant
            }
        ]
    )

# 6. 企业级代理 - 生产环境配置
async def create_enterprise_agent():
    postgres_store = await init_postgres_store()
    
    # 使用 asyncio.to_thread 避免 FilesystemBackend 初始化时的阻塞调用
    docs_backend = await asyncio.to_thread(
        FilesystemBackend,
        root_dir=ENTERPRISE_DOCS_DIR,
        virtual_mode=True
    )
    
    return create_deep_agent(
        model=default_model,
        tools=[],
        system_prompt="""你是一个企业级AI助手，提供安全、可靠、可审计的文件管理服务。
        支持本地文件系统操作、长期数据持久化、详细的操作日志记录。
        适合企业文档管理、合规性要求、多用户协作场景。
        当需要调用外部API时，请委派给tools_Assistant子代理。""",
        backend=lambda rt: CompositeBackend(
            default=StateBackend(rt),
            routes={
                "/documents/": docs_backend,
                "/audit/": StoreBackend(rt),
                "/config/": StoreBackend(rt)
            }
        ),
        store=postgres_store,
        subagents=[
            {
                "name": "tools_Assistant", 
                "description": "专业的API工具调用助手，擅长通过外部API接口获取数据、调用服务和执行远程操作。当我需要获取实时信息、调用第三方服务、访问外部数据源或执行需要API调用的复杂任务时，应该调用此助手。它配备了call_tool和list_resources等API工具，能够处理各种需要外部接口调用的场景。",
                "runnable": tools_Assistant
            }
        ]
    )




