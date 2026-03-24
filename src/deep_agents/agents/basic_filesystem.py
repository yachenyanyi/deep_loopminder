import asyncio
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from src.models.llm import get_default_model
from src.agents.agent import tools_Assistant
from src.deep_agents.config import WORKSPACE_DIR

# 1. 基础文件系统代理 - 安全的本地文件操作
async def create_basic_filesystem_agent():
    # 使用 asyncio.to_thread 避免 FilesystemBackend 初始化时的阻塞调用
    fs_backend = await asyncio.to_thread(
        FilesystemBackend,
        root_dir=WORKSPACE_DIR,  # 使用绝对路径
        virtual_mode=True  # 启用沙盒模式，限制文件访问范围
    )

    return create_deep_agent(
        model=get_default_model(),
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