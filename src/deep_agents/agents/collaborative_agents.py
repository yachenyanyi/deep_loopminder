"""
协作智能体定义

使用 create_deep_agent 创建5个具有明确分工的智能体。
所有代理共享同一个记忆文件（/memories/agent.md）。

代理列表：
1. chat_agent - 通用对话代理（前台接待）
2. coordinator_agent - 协调员代理（项目经理）
3. coder_agent - 代码专家代理（高级工程师）
4. researcher_agent - 研究专家代理（情报分析师）
5. assistant_agent - 个人助理代理（私人秘书）

目录结构：
workspace/
├── memories/
│   └── agent.md      # 共享记忆文件
└── (其他工作文件)     # 代理创建的文件
"""

import os
import asyncio
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend

from src.deep_agents.db import init_postgres_checkpointer, init_postgres_store
from src.deep_agents.config import WORKSPACE_DIR
from src.models.llm import get_default_model
from src.tools.shell_tool import shell_tools
from src.tools.api_tools import call_tool_tool, list_resources_tool
from src.middlewares.agent_communication import AgentCommunicationMiddleware
from src.middlewares.shell.local_shell import local_shell_middleware
from src.deep_agents.agents.employee_registry import COLLABORATIVE_EMPLOYEES


# 确保记忆目录存在
MEMORIES_DIR = os.path.join(WORKSPACE_DIR, "memories")
Path(MEMORIES_DIR).mkdir(parents=True, exist_ok=True)

# 共享的记忆文件路径（虚拟路径）
# 由于 root_dir=WORKSPACE_DIR，代理访问 /memories/agent.md
# 会映射到 WORKSPACE_DIR/memories/agent.md
SHARED_MEMORY_FILE = "/memories/agent.md"


def create_communication_middleware(current_agent_name: str) -> AgentCommunicationMiddleware:
    """为指定代理创建通信中间件"""
    return AgentCommunicationMiddleware(
        server_url="http://127.0.0.1:2024",
        employees=COLLABORATIVE_EMPLOYEES,
        current_employee=current_agent_name,
    )


async def create_agent_backend():
    """创建代理的 backend 配置

    FilesystemBackend(root_dir=WORKSPACE_DIR) 意味着：
    - 虚拟根目录 / 对应实际的 WORKSPACE_DIR

    所以：
    - /memories/agent.md → workspace/memories/agent.md
    - /any_file.txt → workspace/any_file.txt
    """
    fs_backend_instance = await asyncio.to_thread(
        FilesystemBackend,
        root_dir=WORKSPACE_DIR,
        virtual_mode=True
    )

    def backend_factory(runtime):
        return CompositeBackend(
            default=fs_backend_instance,  # 默认使用文件系统后端
            routes={}  # 不需要额外路由，所有路径都在 workspace 下
        )

    return backend_factory


# ============================================================================
# 1. 💬 通用对话代理 (chat_agent)
# ============================================================================

CHAT_AGENT_PROMPT = """你是系统的"前台接待"，负责识别用户意图并决定如何处理。

## 你的职责
1. 处理简单对话：闲聊、常识问答、简单文本生成
2. 意图识别：判断请求的复杂程度
3. 智能路由：将复杂任务转交给正确的专家

## 决策边界
✅ 自己处理：闲聊、简单问答、邮件草稿、情感陪伴、简单文本生成
❌ 立即转交：
- 代码任务 → coder_agent
- 复杂规划 → coordinator_agent
- 信息查询 → researcher_agent
- 日程管理 → assistant_agent

## 使用工具
当你不确定或需要其他专家的意见时，使用 consult_colleague 工具向相应专家咨询。

## 共享记忆
所有代理共享 /memories/agent.md 记忆文件，记录用户偏好和重要信息。
"""


async def create_chat_agent():
    """创建通用对话代理"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()
    backend = await create_agent_backend()

    middleware = create_communication_middleware("chat_agent")

    return create_deep_agent(
        model=get_default_model(),
        tools=[],
        system_prompt=CHAT_AGENT_PROMPT,
        middleware=[middleware, local_shell_middleware],
        memory=[SHARED_MEMORY_FILE],
        backend=backend,
        checkpointer=postgres_checkpointer,
        store=postgres_store,
        name="chat_agent",
    )


# ============================================================================
# 2. 🎯 协调员代理 (coordinator_agent)
# ============================================================================

COORDINATOR_AGENT_PROMPT = """你是系统的"项目经理"，负责协调多个专家完成复杂任务。

## 你的职责
1. 任务拆解：将大目标分解为可执行的原子任务
2. 依赖管理：判断任务并行/串行关系
3. 结果合成：整合各专家输出为最终交付物

## 决策边界
✅ 自己处理：任务规划、进度追踪、最终报告、冲突仲裁
❌ 绝不处理：具体代码、具体搜索、具体文件操作

## 可用的专家
- coder_agent: 代码编写、调试、构建
- researcher_agent: 信息搜索、文档查阅
- assistant_agent: 日程安排、用户偏好

## 共享记忆
所有代理共享 /memories/agent.md 记忆文件，记录项目状态和决策历史。
"""


async def create_coordinator_agent():
    """创建协调员代理"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()
    backend = await create_agent_backend()

    middleware = create_communication_middleware("coordinator_agent")

    return create_deep_agent(
        model=get_default_model(),
        tools=[],
        system_prompt=COORDINATOR_AGENT_PROMPT,
        middleware=[middleware, local_shell_middleware],
        memory=[SHARED_MEMORY_FILE],
        backend=backend,
        checkpointer=postgres_checkpointer,
        store=postgres_store,
        name="coordinator_agent",
    )


# ============================================================================
# 3. 💻 代码专家代理 (coder_agent)
# ============================================================================

CODER_AGENT_PROMPT = """你是系统的"高级工程师"，负责所有代码相关的任务。

## 你的职责
1. 编写/修改代码
2. 运行构建和测试命令
3. 调试和修复问题

## 决策边界
✅ 自己处理：代码编写、命令执行、依赖管理
❌ 立即求助：
- 不懂的业务逻辑 → 咨询 coordinator_agent
- 最新API文档 → 咨询 researcher_agent

## 安全约束

- 危险命令需要确认
- 敏感信息不要硬编码

## 共享记忆
所有代理共享 /memories/agent.md 记忆文件，记录技术栈和代码规范。
"""


async def create_coder_agent():
    """创建代码专家代理"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()
    backend = await create_agent_backend()

    middleware = create_communication_middleware("coder_agent")

    return create_deep_agent(
        model=get_default_model(),
        tools=[],
        system_prompt=CODER_AGENT_PROMPT,
        middleware=[middleware, local_shell_middleware],
        memory=[SHARED_MEMORY_FILE],
        backend=backend,
        checkpointer=postgres_checkpointer,
        store=postgres_store,
        name="coder_agent",
    )


# ============================================================================
# 4. 🔍 研究专家代理 (researcher_agent)
# ============================================================================

RESEARCHER_AGENT_PROMPT = """你是系统的"情报分析师"，负责收集和验证信息。

## 你的职责
1. 网络搜索和信息检索
2. 文档查阅和数据整理
3. 交叉验证信息来源

## 决策边界
✅ 自己处理：搜索、整理、分析、对比
❌ 绝不处理：修改文件、执行命令、猜测未知信息

## 浏览器自动化工具 (playwright-cli)

当需要浏览网页、提取数据、截图时，主动使用 playwright-cli。

### 重要：主动学习用法
**遇到不熟悉的命令时，先查帮助文档**：
```bash
# 查看所有可用命令
playwright-cli --help

# 查看特定命令的详细用法
playwright-cli open --help
playwright-cli snapshot --help
playwright-cli click --help
```

### 快速入门流程
```bash
# 1. 打开浏览器
playwright-cli open https://example.com

# 2. 获取页面快照 → 查看可操作的元素（e1, e2...）
playwright-cli snapshot

# 3. 根据快照中的元素引用进行操作
playwright-cli click e3
playwright-cli fill e5 "搜索内容"

# 4. 截图保存证据
playwright-cli screenshot

# 5. 关闭浏览器
playwright-cli close
```

### 核心原则
1. **先查 help**：不确定用法时执行 `playwright-cli --help` 或 `playwright-cli <命令> --help`
2. **先 snapshot**：操作前获取快照，确认元素引用 (e1, e2...)
3. **后验证**：操作后再次 snapshot 确认结果
4. **必关闭**：完成任务后 `playwright-cli close` 释放资源

## 输出要求
- 必须标注信息来源（URL）
- 多个来源交叉验证
- 按时间戳标注时效性


## 共享记忆
所有代理共享 /memories/agent.md 记忆文件，积累知识库。
"""


async def create_researcher_agent():
    """创建研究专家代理"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()
    backend = await create_agent_backend()

    middleware = create_communication_middleware("researcher_agent")

    return create_deep_agent(
        model=get_default_model(),
        tools=[call_tool_tool, list_resources_tool],
        system_prompt=RESEARCHER_AGENT_PROMPT,
        middleware=[middleware, local_shell_middleware],
        skills=["/skills/playwright-cli/"],
        memory=[SHARED_MEMORY_FILE],
        backend=backend,
        checkpointer=postgres_checkpointer,
        store=postgres_store,
        name="researcher_agent",
    )


# ============================================================================
# 5. 📅 个人助理代理 (assistant_agent)
# ============================================================================

ASSISTANT_AGENT_PROMPT = """你是用户的"私人秘书"，管理个人信息和偏好。

## 你的职责
1. 从对话中提取用户偏好并存储
2. 管理日历和待办事项
3. 提供个性化建议

## 共享记忆系统
所有代理共享 /memories/agent.md 记忆文件，你应该主动维护：

### 记忆内容应包括：
- 用户画像：姓名、职业、联系方式
- 用户偏好：沟通风格、技术栈、常用工具
- 重要日期：生日、纪念日、会议
- 待办事项：需要提醒的任务
- 历史记录：重要的对话摘要

### 更新规则：
每次发现新信息时，更新共享记忆文件。

## 决策边界
✅ 自己处理：日程安排、邮件草稿、提醒设置
❌ 立即确认：发送正式邮件、删除重要数据

## 隐私保护
- 敏感信息（密码、身份证号）不存储
- 重要操作需要用户确认
"""


async def create_assistant_agent():
    """创建个人助理代理"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()
    backend = await create_agent_backend()

    middleware = create_communication_middleware("assistant_agent")

    return create_deep_agent(
        model=get_default_model(),
        tools=[],
        system_prompt=ASSISTANT_AGENT_PROMPT,
        middleware=[middleware, local_shell_middleware],
        memory=[SHARED_MEMORY_FILE],
        backend=backend,
        checkpointer=postgres_checkpointer,
        store=postgres_store,
        name="assistant_agent",
    )