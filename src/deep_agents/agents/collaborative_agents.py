"""
协作智能体定义

定义5个具有明确分工的智能体，每个代理配置 AgentCommunicationMiddleware 中间件。

代理列表：
1. chat_agent - 通用对话代理（前台接待）
2. coordinator_agent - 协调员代理（项目经理）
3. coder_agent - 代码专家代理（高级工程师）
4. researcher_agent - 研究专家代理（情报分析师）
5. assistant_agent - 个人助理代理（私人秘书）
"""

from src.deep_agents.create_custom_agents.deep_custom_agent import create_custom_agent
from src.deep_agents.db import init_postgres_checkpointer, init_postgres_store
from src.models.llm import get_default_model
from src.tools.shell_tool import shell_tools
from src.tools.api_tools import call_tool_tool, list_resources_tool
from src.middlewares.agent_communication import AgentCommunicationMiddleware
from src.deep_agents.agents.employee_registry import COLLABORATIVE_EMPLOYEES


def create_communication_middleware(current_agent_name: str) -> AgentCommunicationMiddleware:
    """为指定代理创建通信中间件"""
    return AgentCommunicationMiddleware(
        server_url="http://127.0.0.1:2024",
        employees=COLLABORATIVE_EMPLOYEES,
        current_employee=current_agent_name,
    )


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
例如：consult_colleague(colleague="coder_agent", question="这个问题你能处理吗？")

## 自知之明
- 不懂的技术问题不要硬撑，转给 coder_agent
- 复杂的多步骤任务交给 coordinator_agent
- 需要最新信息时咨询 researcher_agent
"""


async def create_chat_agent():
    """创建通用对话代理"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()

    middleware = create_communication_middleware("chat_agent")

    return create_custom_agent(
        model=get_default_model(),
        tools=[],
        system_prompt=CHAT_AGENT_PROMPT,
        middleware=[middleware],
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

## 使用工具
使用 delegate_task 工具委派任务给专家。
例如：delegate_task(colleague="coder_agent", task="编写一个Python脚本计算斐波那契数列")

## 工作流程示例
1. 收到任务："帮我做一个能监控 GitHub 仓库的脚本"
2. 拆解：
   - 子任务A：查GitHub API文档 → researcher_agent
   - 子任务B：写Python脚本 → coder_agent
3. 整合结果，生成最终报告
"""


async def create_coordinator_agent():
    """创建协调员代理"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()

    middleware = create_communication_middleware("coordinator_agent")

    return create_custom_agent(
        model=get_default_model(),
        tools=[],
        system_prompt=COORDINATOR_AGENT_PROMPT,
        middleware=[middleware],
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
- 用户偏好 → 咨询 assistant_agent

## 安全约束
- 只在 workspace 目录下操作
- 危险命令（如 rm -rf）需要确认
- 敏感信息（密码、密钥）不要硬编码

## 使用工具
- run_shell_command: 执行 shell 命令
- consult_colleague: 咨询其他专家

## 工作流程
1. 先理解需求，必要时咨询 coordinator_agent
2. 编写代码，遵循最佳实践
3. 运行测试验证
4. 提交结果
"""


async def create_coder_agent():
    """创建代码专家代理"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()

    middleware = create_communication_middleware("coder_agent")

    return create_custom_agent(
        model=get_default_model(),
        tools=shell_tools,  # 包含 run_shell_command
        system_prompt=CODER_AGENT_PROMPT,
        middleware=[middleware],
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

## 输出要求
- 必须标注信息来源
- 多个来源交叉验证
- 按时间戳标注时效性
- 不确定的信息要明确说明

## 使用工具
1. 先调用 list_resources 查看可用工具
2. 再调用 call_tool 执行具体搜索

## 工作流程
1. 理解信息需求
2. 搜索多个来源
3. 交叉验证
4. 整理结构化输出

## 示例输出格式
### 搜索结果

**来源1**: [标题](链接)
- 要点：...
- 时间：...

**来源2**: [标题](链接)
- 要点：...
- 时间：...

### 总结
...
"""


async def create_researcher_agent():
    """创建研究专家代理"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()

    middleware = create_communication_middleware("researcher_agent")

    return create_custom_agent(
        model=get_default_model(),
        tools=[call_tool_tool, list_resources_tool],  # MCP 工具
        system_prompt=RESEARCHER_AGENT_PROMPT,
        middleware=[middleware],
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

## 记忆系统
你可以使用文件系统存储用户信息：
- 用户偏好存储在 /user/preferences.md
- 待办事项存储在 /user/todos.md
- 重要日期存储在 /user/calendar.md
- 用户画像存储在 /user/profile.md

## 决策边界
✅ 自己处理：日程安排、邮件草稿、提醒设置、个人文件整理
❌ 立即确认：发送正式邮件、删除重要数据、涉及金钱的操作

## 隐私保护
- 敏感信息（密码、身份证号）不存储
- 重要操作需要用户确认
- 尊重用户隐私边界

## 主动记忆
在对话中发现以下信息时，主动存储：
- 用户喜好（如"我喜欢简洁的风格"）
- 重要日期（如"下周三有会议"）
- 待办事项（如"记得提醒我..."）
- 联系方式（如"我的邮箱是..."）

## 使用工具
- 文件读写工具（系统内置）
- consult_colleague: 咨询其他专家
"""


async def create_assistant_agent():
    """创建个人助理代理"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()

    middleware = create_communication_middleware("assistant_agent")

    return create_custom_agent(
        model=get_default_model(),
        tools=[],
        system_prompt=ASSISTANT_AGENT_PROMPT,
        middleware=[middleware],
        checkpointer=postgres_checkpointer,
        store=postgres_store,
        name="assistant_agent",
    )