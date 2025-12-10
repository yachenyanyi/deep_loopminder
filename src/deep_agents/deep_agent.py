import os
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
from langgraph.checkpoint.memory import InMemorySaver
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

# 2. 临时状态代理 - 会话级别的临时存储
State_Only_Agent = create_deep_agent(
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
Persistent_Memory_Agent = create_deep_agent(
    model=default_model,
    tools=[],
    system_prompt="""你是一个具有长期记忆的AI助手，能够跨会话保存和检索信息。
    你可以创建持久的笔记、知识库、项目文档，这些信息会在不同会话间保持。
    适合构建个人知识管理系统、项目跟踪、长期学习记录。
    当需要调用外部API时，请委派给tools_Assistant子代理。""",
    backend=lambda rt: StoreBackend(rt),  # 使用StoreBackend进行持久化存储
    store=InMemoryStore(),  # 提供BaseStore实例
    subagents=[
        {
            "name": "tools_Assistant", 
            "description": "专业的API工具调用助手，擅长通过外部API接口获取数据、调用服务和执行远程操作。当我需要获取实时信息、调用第三方服务、访问外部数据源或执行需要API调用的复杂任务时，应该调用此助手。它配备了call_tool和list_resources等API工具，能够处理各种需要外部接口调用的场景。",
            "runnable": tools_Assistant
        }
    ]
)

# 4. 混合存储代理 - 智能路由不同存储后端
Hybrid_Storage_Agent = create_deep_agent(
    model=default_model,
    tools=[],
    system_prompt="""你是一个智能存储管理助手，能够根据文件类型自动选择合适的存储方式。
    /tmp/ 路径下的文件为临时文件，/memories/ 路径下的文件会永久保存，
    其他文件存储在当前会话中。这种混合方式既保证了性能又提供了持久化能力。
    当需要调用外部API时，请委派给tools_Assistant子代理。""",
    backend=lambda rt: CompositeBackend(
        default=StateBackend(rt),  # 默认使用StateBackend
        routes={
            "/tmp/": StateBackend(rt),  # 临时文件使用StateBackend
            "/memories/": StoreBackend(rt),  # 记忆文件使用StoreBackend持久化
            "/workspace/": FilesystemBackend(  # 工作文件使用本地文件系统
                root_dir=os.path.join(os.getcwd(), "workspace"),
                virtual_mode=True
            )
        }
    ),
    store=InMemoryStore(),
    subagents=[
        {
            "name": "tools_Assistant", 
            "description": "专业的API工具调用助手，擅长通过外部API接口获取数据、调用服务和执行远程操作。当我需要获取实时信息、调用第三方服务、访问外部数据源或执行需要API调用的复杂任务时，应该调用此助手。它配备了call_tool和list_resources等API工具，能够处理各种需要外部接口调用的场景。",
            "runnable": tools_Assistant
        }
    ]
)

# 5. 高性能分析代理 - 针对大数据处理优化
Analytics_Agent = create_deep_agent(
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
Enterprise_Agent = create_deep_agent(
    model=default_model,
    tools=[],
    system_prompt="""你是一个企业级AI助手，提供安全、可靠、可审计的文件管理服务。
    支持本地文件系统操作、长期数据持久化、详细的操作日志记录。
    适合企业文档管理、合规性要求、多用户协作场景。
    当需要调用外部API时，请委派给tools_Assistant子代理。""",
    backend=lambda rt: CompositeBackend(
        default=StateBackend(rt),
        routes={
            "/documents/": FilesystemBackend(
                root_dir=os.path.join(os.getcwd(), "enterprise_docs"),
                virtual_mode=True
            ),
            "/audit/": StoreBackend(rt),
            "/config/": StoreBackend(rt)
        }
    ),
    store=InMemoryStore(),
    subagents=[
        {
            "name": "tools_Assistant", 
            "description": "专业的API工具调用助手，擅长通过外部API接口获取数据、调用服务和执行远程操作。当我需要获取实时信息、调用第三方服务、访问外部数据源或执行需要API调用的复杂任务时，应该调用此助手。它配备了call_tool和list_resources等API工具，能够处理各种需要外部接口调用的场景。",
            "runnable": tools_Assistant
        }
    ]
)

# 原有的智能深度助手（保留兼容性）
Intelligent_Deep_Assistant = create_deep_agent(
    model=default_model,
    tools=[],#call_tool_tool, list_resources_tool
    system_prompt="你是一个高级AI助手，当需要查询文档或者调用外部API或工具时，请委派给 tools_Assistant 子代理处理。",
    backend=FilesystemBackend(
        root_dir=os.path.join(os.getcwd(), "workspace"),  # 使用绝对路径
        virtual_mode=True
    ),  
    
    # middleware=[full_featured_summary, todo_middleware], # Removed to avoid duplicate middleware error as create_deep_agent adds them by default
    subagents=[
        {
            "name": "tools_Assistant", 
            "description": "专业的API工具调用助手，擅长通过外部API接口获取数据、调用服务和执行远程操作。当我需要获取实时信息、调用第三方服务、访问外部数据源或执行需要API调用的复杂任务时，应该调用此助手。它配备了call_tool和list_resources等API工具，能够处理各种需要外部接口调用的场景。",
            "runnable": tools_Assistant
        }
    ]
)


def get_agent_by_use_case(use_case: str):
    """
    根据使用场景获取合适的代理实例
    
    Args:
        use_case: 使用场景名称
        
    Returns:
        对应的代理实例
        
    Available use cases:
        - basic_filesystem: 基础文件系统操作
        - state_only: 临时状态存储
        - persistent_memory: 持久化记忆
        - hybrid_storage: 混合存储
        - analytics: 数据分析
        - enterprise: 企业级应用
        - role_playing: 角色扮演与长对话记忆
        - intelligent_deep: 智能深度助手（默认）
    """
    agents = {
        "basic_filesystem": Basic_Filesystem_Agent,
        "state_only": State_Only_Agent,
        "persistent_memory": Persistent_Memory_Agent,
        "hybrid_storage": Hybrid_Storage_Agent,
        "analytics": Analytics_Agent,
        "enterprise": Enterprise_Agent,
        "role_playing": Role_Playing_Agent,
        "intelligent_deep": Intelligent_Deep_Assistant
    }
    return agents.get(use_case, Intelligent_Deep_Assistant)


def list_all_agents():
    """列出所有可用的代理类型"""
    return {
        "Basic_Filesystem_Agent": "基础文件系统代理 - 安全的本地文件操作",
        "State_Only_Agent": "临时状态代理 - 会话级别的临时存储", 
        "Persistent_Memory_Agent": "持久化存储代理 - 跨会话的长期记忆",
        "Hybrid_Storage_Agent": "混合存储代理 - 智能路由不同存储后端",
        "Analytics_Agent": "高性能分析代理 - 针对大数据处理优化",
        "Enterprise_Agent": "企业级代理 - 生产环境配置",
        "Intelligent_Deep_Assistant": "智能深度助手 - 原有的综合代理",
        "Role_Playing_Agent": "角色扮演代理 - 长对话记忆与性格保持"
    }


# 7. 角色扮演代理 - 长对话记忆与性格保持
Role_Playing_Agent = create_deep_agent(
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
    
    # 使用内存存储作为BaseStore实例
    store=InMemoryStore(),
    
    # 配置checkpointer用于线程级别的对话记忆
    checkpointer=InMemorySaver(),
    
    # 启用流式响应以优化用户体验和token使用
    stream_mode=["messages", "updates"],
    
    # 子代理配置
    subagents=[
        {
            "name": "tools_Assistant", 
            "description": "专业的API工具调用助手，当我需要获取外部信息或执行复杂任务时提供支持。",
            "runnable": tools_Assistant
        }
    ],
    
    # 角色扮演特定的中间件配置
    middleware=[
        # 可以添加角色扮演特定的中间件，如情感分析、性格一致性检查等
    ]
)

