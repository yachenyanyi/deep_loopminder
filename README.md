# Deep Loopminder

基于 LangGraph 的协作智能体系统，模拟 AI 公司运作模式。每个代理就像公司的员工，有明确的职责分工，可以相互通信、协作完成复杂任务。

## 核心亮点

### 🏢 AI 公司模式
代理各司其职，像公司员工一样协作。chat_agent 作为前台接待，自动识别用户意图并路由到合适的专家。

```
用户请求 → chat_agent (前台接待)
              ↓ 意图识别 & 智能路由
         ┌────┼────┐
         ↓    ↓    ↓
    coder  coordinator  researcher
    (工程师)  (经理)     (分析师)
```

### 🤝 代理间通信 (A2A 协议)
- `consult_colleague` 工具实现代理间咨询
- 每个代理有独立的对话线程
- 任务自动转交和协作

### 🧠 共享记忆系统
- 所有代理共享 `/memories/agent.md` 记忆文件
- 知识跨代理传递（用户偏好、技术栈、重要信息）
- 长期记忆积累，越用越智能

### 🛡️ 安全与高可用
- 虚拟文件系统隔离（代理只能访问 workspace 目录）
- PostgreSQL 持久化 + 自动内存回退
- 懒加载模型（按需创建实例，降低启动开销）
- 环境变量安全传递

### 🛠️ 可扩展架构
- 基于技能(Skills)的能力扩展
- 中间件模式（Shell、通信、日志）
- 灵活的工具配置

## 代理介绍

| 代理 | 角色 | 职责 | 工具 |
|------|------|------|------|
| `chat_agent` | 前台接待 | 意图识别、简单对话、智能路由 | Shell |
| `coordinator_agent` | 项目经理 | 任务拆解、依赖管理、结果合成 | Shell |
| `coder_agent` | 高级工程师 | 代码编写、命令执行、调试修复 | Shell |
| `researcher_agent` | 情报分析师 | 信息检索、文档查阅、交叉验证 | Shell + API Tools + Playwright |
| `assistant_agent` | 私人秘书 | 日程管理、偏好记录、个性化建议 | Shell |

## 架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                        LangGraph Server                          │
│                      http://localhost:2024                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐         │
│   │chat_agent│ │coordinator│ │coder_    │ │researcher│         │
│   │          │ │  _agent  │ │ agent    │ │  _agent  │         │
│   └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘         │
│        │            │            │            │                 │
│        └────────────┴────────────┴────────────┘                 │
│                          │                                       │
│              ┌───────────┴───────────┐                          │
│              │  AgentCommunication   │                          │
│              │     Middleware        │                          │
│              │   (A2A Protocol)      │                          │
│              └───────────────────────┘                          │
│                                                                  │
├─────────────────────────────────────────────────────────────────┤
│                      Middlewares & Tools                         │
│  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐   │
│  │ShellToolMiddleware│ │   Memory File   │ │     Skills      │   │
│  │   (local_shell)  │ │ /memories/agent.md│ │  /skills/...   │   │
│  └─────────────────┘ └─────────────────┘ └─────────────────┘   │
├─────────────────────────────────────────────────────────────────┤
│                       Storage Layer                              │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │            FilesystemBackend (workspace/)               │    │
│  │   虚拟根目录 / → 实际路径 workspace/                     │    │
│  └─────────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │     PostgreSQL Checkpointer/Store (可选，支持内存回退)   │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

## 快速开始

### 环境要求

- Python >= 3.12
- PostgreSQL (可选，用于持久化存储)
- Node.js (用于 playwright-cli skill)

### 安装

```bash
# 克隆项目
git clone https://github.com/yachenyanyi/deep_loopminder.git
cd deep_loopminder

# 安装依赖
pip install -e .

# 安装 playwright-cli (可选，用于研究代理)
npm install -g @anthropic/playwright-cli
```

### 配置

创建 `.env` 文件：

```bash
# LLM API (二选一)
OPENAI_API_KEY=your_openai_key
# 或
ANTHROPIC_API_KEY=your_anthropic_key

# PostgreSQL (可选，不配置则使用内存存储)
POSTGRES_DB_URI=postgresql://user:password@localhost:5432/dbname
```

### 启动

```bash
# 启动 LangGraph 开发服务器
langgraph dev

# 服务器地址
# API: http://localhost:2024
# Studio UI: https://smith.langchain.com/studio/?baseUrl=http://localhost:2024
```

## 使用示例

### 场景1: 简单问答

用户向 chat_agent 发起简单对话，chat_agent 直接处理：

```python
from langgraph_sdk import get_client

async def simple_chat():
    client = get_client(url="http://127.0.0.1:2024")

    # 创建线程
    thread = await client.threads.create()

    # 发送消息
    response = await client.runs.wait(
        thread_id=thread["thread_id"],
        assistant_id="chat_agent",
        input={"messages": [{"role": "user", "content": "你好，介绍一下你自己"}]}
    )

    print(response["messages"][-1]["content"])
```

### 场景2: 多代理协作

复杂任务自动路由到合适的专家，代理间协作完成：

```python
from langgraph_sdk import get_client

async def collaborative_task():
    client = get_client(url="http://127.0.0.1:2024")
    thread = await client.threads.create()

    # 用户请求需要多个代理协作
    response = await client.runs.wait(
        thread_id=thread["thread_id"],
        assistant_id="chat_agent",  # 从 chat_agent 开始
        input={"messages": [{
            "role": "user",
            "content": "帮我写一个 Python 爬虫，抓取 example.com 的标题"
        }]}
    )

    # 执行流程：
    # 1. chat_agent 识别为代码任务 → 转交 coder_agent
    # 2. coder_agent 需要了解网站结构 → 咨询 researcher_agent
    # 3. researcher_agent 提供 API 文档信息
    # 4. coder_agent 完成代码编写

    print(response["messages"][-1]["content"])
```

### 场景3: 直接调用专家代理

也可以直接调用特定专家：

```python
async def direct_expert():
    client = get_client(url="http://127.0.0.1:2024")
    thread = await client.threads.create()

    # 直接调用代码专家
    response = await client.runs.wait(
        thread_id=thread["thread_id"],
        assistant_id="coder_agent",
        input={"messages": [{
            "role": "user",
            "content": "帮我写一个 hello.py 文件"
        }]}
    )
```

## 目录结构

```
deep_loopminder/
├── src/
│   ├── deep_agents/
│   │   ├── agents/
│   │   │   ├── collaborative_agents.py  # 5个协作代理定义
│   │   │   └── employee_registry.py     # 员工注册表
│   │   ├── config.py                    # 全局配置
│   │   └── db.py                        # 数据库连接 + 回退机制
│   ├── middlewares/
│   │   ├── agent_communication.py       # A2A 通信中间件
│   │   ├── thread_config.py             # 线程配置管理
│   │   └── shell/
│   │       ├── local_shell.py           # 本地 Shell 中间件
│   │       └── web_shell.py             # Web Shell 中间件
│   ├── models/
│   │   └── llm.py                       # 懒加载模型
│   └── tools/
│       ├── shell_tool.py                # Shell 工具
│       └── api_tools.py                 # API 调用工具
├── workspace/                           # 代理工作目录（虚拟文件系统）
│   ├── memories/
│   │   └── agent.md                    # 共享记忆文件
│   └── skills/                         # Skill 存放目录
├── langgraph.json                       # LangGraph 配置
├── pyproject.toml                       # 项目配置
└── README.md
```

## 虚拟文件系统

代理的文件操作被限制在 `workspace/` 目录内：

| 虚拟路径 | 实际路径 |
|---------|---------|
| `/` | `workspace/` |
| `/memories/agent.md` | `workspace/memories/agent.md` |
| `/test.py` | `workspace/test.py` |

这样设计确保：
- 代理无法访问主机敏感文件
- 多个代理可以安全共享工作空间
- 便于管理和清理代理创建的文件

## 技术栈

- [LangGraph](https://github.com/langchain-ai/langgraph) - 状态图框架
- [DeepAgents](https://github.com/langchain-ai/deepagents) - 深度代理框架
- [PostgreSQL](https://www.postgresql.org/) - 持久化存储（可选）
- [playwright-cli](https://www.npmjs.com/package/@anthropic/playwright-cli) - 浏览器自动化 skill

## License

MIT

---

**注意**: 本项目正在积极开发中，API 和功能可能会发生变化。