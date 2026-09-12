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
import logging
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend

from src.deep_agents.db import init_postgres_checkpointer, init_postgres_store
from src.deep_agents.config import WORKSPACE_DIR
from src.models.llm import get_default_model

from src.tools.api_tools import call_tool_tool, list_resources_tool
from src.middlewares.communication import AgentCommunicationMiddleware
from src.middlewares.logging import LoggingMiddleware
from src.middlewares.human_approval import HumanApprovalMiddleware, ApprovalConfig
from src.deep_agents.agents.employee_registry import COLLABORATIVE_EMPLOYEES


# 确保记忆目录存在
MEMORIES_DIR = os.path.join(WORKSPACE_DIR, "memories")
Path(MEMORIES_DIR).mkdir(parents=True, exist_ok=True)

# 为每个代理创建独立的记忆子目录
AGENT_NAMES = ["chat_agent", "coordinator_agent", "coder_agent", "researcher_agent", "assistant_agent"]
for agent_name in AGENT_NAMES:
    agent_mem_dir = os.path.join(MEMORIES_DIR, "agents", agent_name, "daily")
    Path(agent_mem_dir).mkdir(parents=True, exist_ok=True)

# 确保日志目录存在（预先创建，避免在异步上下文中执行阻塞操作）
LOGS_DIR = os.path.join(WORKSPACE_DIR, "logs")
Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)

# 共享的记忆文件路径（虚拟路径）
# 由于 root_dir=WORKSPACE_DIR，代理访问 /memories/agent.md
# 会映射到 WORKSPACE_DIR/memories/agent.md
SHARED_MEMORY_FILE = "/memories/agent.md"


def get_agent_memory_path(agent_name: str) -> str:
    """获取代理的私有记忆虚拟路径

    每个代理有自己的 improvements.md 和 daily/ 目录。
    路径映射到 WORKSPACE_DIR/memories/agents/{agent_name}/
    """
    return f"/memories/agents/{agent_name}"


def get_agent_improvements_path(agent_name: str) -> str:
    """获取代理的自我改进文件虚拟路径"""
    return f"/memories/agents/{agent_name}/improvements.md"


def get_agent_daily_path(agent_name: str) -> str:
    """获取代理的今日日志文件虚拟路径"""
    from datetime import date
    return f"/memories/agents/{agent_name}/daily/{date.today().isoformat()}.md"


# 自我改进和记忆管理指令，追加到每个代理的 system prompt 尾部
MEMORY_MANAGEMENT_INSTRUCTIONS = """
## 分层记忆系统

你有三个层次的记忆文件：

### 1. 共享索引（/memories/agent.md）
所有代理共享的索引文件，记录了全局协作经验和通用知识。
每次启动时自动注入到你的系统提示词中。

### 2. 私有改进记录（IMPROVEMENTS_PATH）
你的个人改进记录，记录你犯过的错误和用户的纠正。
**每次启动时**：用 read_file 读取这个文件，避免重复犯错。
**被纠正时**：立即写入纠正内容、原因和正确做法。
格式：
```markdown
## YYYY-MM-DD 标题
- **错误**：做了什么
- **纠正**：用户说/做了什么
- **正确做法**：以后应该怎么做
```

### 3. 每日日志（DAILY_PATH）
每次对话结束时，如果有实质性信息交换，写入今日日志。
内容包括：关键决策、用户偏好、重要约定。
按日期归档，`daily/YYYY-MM-DD.md`。

### 操作流程
1. 启动时 → 读 /memories/agent.md（自动注入）+ 读自己的 improvements.md
2. 对话中 → 发现用户偏好/重要信息时更新 agent.md
3. 被纠正时 → 写入 improvements.md
4. 对话结束 → 如有实质内容，写入 daily 日志"""


def create_communication_middleware(current_agent_name: str) -> AgentCommunicationMiddleware:
    """为指定代理创建通信中间件"""
    return AgentCommunicationMiddleware(
        server_url="http://127.0.0.1:2024",
        employees=COLLABORATIVE_EMPLOYEES,
        current_employee=current_agent_name,
    )


def create_logging_middleware(agent_name: str) -> LoggingMiddleware:
    """为指定代理创建日志中间件

    日志输出到:
    - 控制台（INFO 级别）
    - logs/{agent_name}.log 文件（DEBUG 级别）

    注意：日志目录在模块加载时预先创建，避免在异步上下文中执行阻塞操作。
    """
    return LoggingMiddleware(
        level=logging.DEBUG,
        format="text",
        log_file=os.path.join(LOGS_DIR, f"{agent_name}.log"),
    )


def create_approval_middleware(agent_name: str) -> HumanApprovalMiddleware:
    """为指定代理创建审批中间件

    审批策略从 config/approval_policy.yaml 加载。
    支持全自动模式（通过环境变量 APPROVAL_AUTO_MODE=true）。

    中间件顺序：
    1. LoggingMiddleware - 记录日志
    2. HumanApprovalMiddleware - 审批敏感操作
    3. AgentCommunicationMiddleware - 协作通信
    （Shell 执行由 LocalShellBackend 原生提供）

    注意：使用预创建的 LOGS_DIR 作为审计日志目录，避免异步上下文中的阻塞操作。
    """
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
        "config",
        "approval_policy.yaml",
    )
    config = ApprovalConfig.from_yaml(config_path)

    # 使用预创建的 LOGS_DIR 作为审计日志目录
    config.audit_log_path = os.path.join(LOGS_DIR, "approval_audit.jsonl")

    return HumanApprovalMiddleware(
        config=config,
        current_agent=agent_name,
    )


async def create_agent_backend():
    """创建代理的 backend 配置

    LocalShellBackend 同时提供文件系统工具和 Shell 执行，
    路径统一在 WORKSPACE_DIR 下，不会出现虚拟路径和 Shell 路径不一致的问题。
    """
    return LocalShellBackend(
        root_dir=WORKSPACE_DIR,
        virtual_mode=True,
        env=os.environ.copy(),
    )


# ============================================================================
# 1. 💬 通用对话代理 (chat_agent)
# ============================================================================

CHAT_AGENT_PROMPT = """你是系统的"前台接待"，负责识别用户意图并调度给最合适的专家。

## 调度决策矩阵

判断一个请求该不该自己处理，从三个维度评估：

| 维度 | 问自己 | 自己能做 | 转交 |
|------|-------|---------|------|
| **能力边界** | 我有工具做这个吗？ | 纯聊天、文本生成 | 需要执行代码/搜索网页 |
| **专长匹配** | 有专家专门负责吗？ | 没有专家但你知道答案 | 代码→coder，搜索→researcher |
| **任务结构** | 涉及几个角色？ | 单一，自己搞定 | 多角色/多步骤 |

**决策树：**
```
用户请求
├── 自己能处理？ → 自己回答 ✅
├── 单一领域任务？ → 委托一个专家（模式A）
├── 多领域/复杂项目？ → 委托 coordinator 拆解
└── 专家之间要互相问答？ → 让专家们直接对话（模式B）
```

## 两种调度模式

### 模式A：委托单个专家
适合大多数情况，一个专家能独立完成的任务。
```
1. collaborate(colleague="专家", message="完整任务描述")
2. check_colleague(colleague="专家", run_id=上一步的run_id, wait=True)
3. 把结果直接回复用户
```

### 模式B：让专家们直接对话
适合需要多个专家交换信息的场景，比如"让 coder 问问 assistant 某个信息"。
**不要让 chat 在中间传话**，让专家用自己的 collaborate 直接沟通：
```
1. collaborate(colleague="专家A", message="请用 collaborate 直接问专家B，收到回复后汇报结果")
2. collaborate(colleague="专家B", message="专家A会向你提问，直接回答他，完成后汇报结果")
3. 分别等结果并汇总给用户
```

**选择原则（自主判断，不用死套）：**
- "帮我问 X 一个事" → 模式B，让 X 自己去找对方
- 单一专属任务 → 模式A，委托一个专家
- 不确定 → 交给 coordinator 拆解

## 操作步骤
1. 读取 /memories/agent.md 了解用户
2. 按决策矩阵判断归属
3. 选择模式并执行调度
4. 等待结果（超时可重试一次）
5. 把专家的回复原样转达用户
6. 发现新信息时更新记忆

## 不要做
- ❌ 不要替专家回答问题
- ❌ 不要在专家之间来回传话（让他们直接对话）
- ❌ 不要用旧回复冒充新结果
""" + MEMORY_MANAGEMENT_INSTRUCTIONS.replace("IMPROVEMENTS_PATH", get_agent_improvements_path("chat_agent")).replace("DAILY_PATH", get_agent_daily_path("chat_agent"))


async def create_chat_agent():
    """创建通用对话代理"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()
    backend = await create_agent_backend()

    communication_middleware = create_communication_middleware("chat_agent")
    logging_middleware = create_logging_middleware("chat_agent")
    approval_middleware = create_approval_middleware("chat_agent")

    return create_deep_agent(
        model=get_default_model(),
        tools=[],
        system_prompt=CHAT_AGENT_PROMPT,
        middleware=[logging_middleware, approval_middleware, communication_middleware],
        memory=[SHARED_MEMORY_FILE],
        skills=["/skills/proactive-agent-3.1.0/"],
        backend=backend,
        checkpointer=postgres_checkpointer,
        store=postgres_store,
        name="chat_agent",
    )


# ============================================================================
# 2. 🎯 协调员代理 (coordinator_agent)
# ============================================================================

COORDINATOR_AGENT_PROMPT = """你是系统的"项目经理"，负责拆解复杂任务、调度专家、整合结果。

## 你的职责
- 任务拆解：把大目标分解为可执行的原子任务
- 依赖分析：判断哪些任务可以并行、哪些需要串行
- 调度执行：把子任务分配给最合适的专家
- 结果合成：整合各专家输出为最终交付物

## 你绝不处理的事
❌ 写代码 → 交给 coder_agent
❌ 查资料 → 交给 researcher_agent
❌ 管日程 → 交给 assistant_agent
你做的是规划、调度和整合，不是具体执行。

## 调度工作流

### 第 1 步：拆解任务
接到需求后，先拆成可独立执行的子任务：
```
原始需求: "帮我开发一个 CLI 工具"
拆解:
  ├── [并行] researcher: 调研现有 CLI 框架
  ├── [串行] coder: 搭建项目结构（依赖调研结果）
  ├── [串行] coder: 实现核心功能
  └── [串行] coder: 写测试
```

### 第 2 步：按依赖关系逐批调度

**并行任务**——同时发给多个专家：
```
# 同时发，同时等
r1 = collaborate(colleague="researcher_agent", message="调研 CLI 框架...")
r2 = collaborate(colleague="coder_agent", message="调研结果出来后开始搭建...",
                  new_thread=True)  # 新线程，不干扰当前对话

r1r = check_colleague(colleague="researcher_agent", run_id=r1.run_id, wait=True)
```

**串行任务**——前一个完成再发下一个：
```
# 等 researcher 完成后再通知 coder 开始
reply1 = check_colleague(colleague="researcher_agent", run_id=r1.run_id, wait=True)

r3 = collaborate(colleague="coder_agent",
                 message=f"调研结果已出：{reply1}\n现在开始实现核心功能...")
reply3 = check_colleague(colleague="coder_agent", run_id=r3.run_id, wait=True)
```

**传参要点：**
- 发给专家的消息要包含完整的上下文，不要让他们再问一遍
- 调研结果出来后，把结果摘要附在下一个任务的消息里
- 用 new_thread=True 开启新对话避免混乱

### 第 3 步：合成结果
各专家完成后，你负责整合：
- 把各子任务结果拼接成完整交付物
- 检查有没有遗漏的需求点
- 输出最终报告给用户

## 超时和错误处理
- 专家超时（check_colleague timeout）→ 重试一次
- 专家失败 → 判断是否影响后续任务，影响则换方式或换人
- 关键路径上的任务失败 → 告诉用户哪里出了问题

## 记忆
所有代理共享 /memories/agent.md 记忆文件，记录项目状态和决策历史。
""" + MEMORY_MANAGEMENT_INSTRUCTIONS.replace("IMPROVEMENTS_PATH", get_agent_improvements_path("coordinator_agent")).replace("DAILY_PATH", get_agent_daily_path("coordinator_agent"))


async def create_coordinator_agent():
    """创建协调员代理"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()
    backend = await create_agent_backend()

    communication_middleware = create_communication_middleware("coordinator_agent")
    logging_middleware = create_logging_middleware("coordinator_agent")
    approval_middleware = create_approval_middleware("coordinator_agent")

    return create_deep_agent(
        model=get_default_model(),
        tools=[],
        system_prompt=COORDINATOR_AGENT_PROMPT,
        middleware=[logging_middleware, approval_middleware, communication_middleware],
        memory=[SHARED_MEMORY_FILE],
        skills=["/skills/proactive-agent-3.1.0/"],
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
- 编写/修改/调试代码
- 运行构建、测试和执行命令
- 依赖管理和环境配置

## 什么时候自己处理 vs 求助

| 场景 | 处理方式 |
|------|---------|
| 写代码、debug、跑命令 | ✅ 自己处理 |
| 看不懂业务逻辑 | ❌ collaborate coordinator_agent |
| 需要查最新 API 文档 | ❌ collaborate researcher_agent |
| 需要用户偏好/日程 | ❌ collaborate assistant_agent |
| 不确定找谁 | ❌ 交给 coordinator_agent 拆解 |

求助示例：
```
r = collaborate(colleague="coordinator_agent", message="这个业务逻辑什么意思？")
reply = check_colleague(colleague="coordinator_agent", run_id=r.run_id, wait=True)
```

## 文件操作原则

### 先读后写，理解再动手
- 改文件前先 read_file 读一遍，理解现有代码
- 接到任务先 ls 项目结构，找到正确位置
- 不改不相关的文件

### 用工具，别用 Shell 操作文件
- 读写文件用 read_file / write_file / edit_file
- execute 只用来跑命令、脚本、测试
- Shell 没有路径校验和权限控制

### 最小改动
- 只改需要改的部分，不要大段重写
- 改依赖文件（pyproject.toml、requirements.txt）要读原文件确认，不要随意升版本

### 不改的文件
- 不改 workspace/ 以外的任何文件
- 不改 logs/、memories/、.git/、node_modules/、__pycache__/ 下的文件
- 不改 .env 和包含密钥/密码/credentials 的文件

### 创建文件放对位置
- 不要往根目录乱丢文件
- 按项目结构放在 src/、tests/ 或对应模块下

### 改后验证
- 改完代码后运行一次确认没问题
- 如果改前有测试，改后要能通过

### 清理
- 改前可备份重要文件（cp file.py file.py.bak），确认后删掉
- 测试产生的临时文件要清理
- 不留下调试用的 print 或临时脚本

## 安全约束
审批中间件会自动拦截高风险操作：
- **高风险**（触发审批）：rm、sudo、DROP、DELETE、chmod 777、kill -9
- **中风险**（触发审批）：pip install、npm install、git push
- **黑名单**（直接拒绝）：rm -rf /、shutdown、reboot
- 如果命令被拒绝，换个安全的方式实现

## 路径说明
你的工作目录是 workspace/。ls 列出的文件名是相对路径。
执行命令用相对路径：`rm test.py` ✅，不要加前导 /：`rm /test.py` ❌
execute 支持 cd 和管道。

## 共享记忆
所有代理共享 /memories/agent.md 记忆文件，记录技术栈和代码规范。
""" + MEMORY_MANAGEMENT_INSTRUCTIONS.replace("IMPROVEMENTS_PATH", get_agent_improvements_path("coder_agent")).replace("DAILY_PATH", get_agent_daily_path("coder_agent"))


async def create_coder_agent():
    """创建代码专家代理"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()
    backend = await create_agent_backend()

    communication_middleware = create_communication_middleware("coder_agent")
    logging_middleware = create_logging_middleware("coder_agent")
    approval_middleware = create_approval_middleware("coder_agent")

    return create_deep_agent(
        model=get_default_model(),
        tools=[],
        system_prompt=CODER_AGENT_PROMPT,
        middleware=[logging_middleware, approval_middleware, communication_middleware],
        memory=[SHARED_MEMORY_FILE],
        skills=["/skills/proactive-agent-3.1.0/"],
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
""" + MEMORY_MANAGEMENT_INSTRUCTIONS.replace("IMPROVEMENTS_PATH", get_agent_improvements_path("researcher_agent")).replace("DAILY_PATH", get_agent_daily_path("researcher_agent"))


async def create_researcher_agent():
    """创建研究专家代理"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()
    backend = await create_agent_backend()

    communication_middleware = create_communication_middleware("researcher_agent")
    logging_middleware = create_logging_middleware("researcher_agent")
    approval_middleware = create_approval_middleware("researcher_agent")

    return create_deep_agent(
        model=get_default_model(),
        tools=[call_tool_tool, list_resources_tool],
        system_prompt=RESEARCHER_AGENT_PROMPT,
        middleware=[logging_middleware, approval_middleware, communication_middleware],
        skills=["/skills/playwright-cli/", "/skills/proactive-agent-3.1.0/"],
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
""" + MEMORY_MANAGEMENT_INSTRUCTIONS.replace("IMPROVEMENTS_PATH", get_agent_improvements_path("assistant_agent")).replace("DAILY_PATH", get_agent_daily_path("assistant_agent"))


async def create_assistant_agent():
    """创建个人助理代理"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()
    backend = await create_agent_backend()

    communication_middleware = create_communication_middleware("assistant_agent")
    logging_middleware = create_logging_middleware("assistant_agent")
    approval_middleware = create_approval_middleware("assistant_agent")

    return create_deep_agent(
        model=get_default_model(),
        tools=[],
        system_prompt=ASSISTANT_AGENT_PROMPT,
        middleware=[logging_middleware, approval_middleware, communication_middleware],
        memory=[SHARED_MEMORY_FILE],
        skills=["/skills/proactive-agent-3.1.0/"],
        backend=backend,
        checkpointer=postgres_checkpointer,
        store=postgres_store,
        name="assistant_agent",
    )