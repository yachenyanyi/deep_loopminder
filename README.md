# Deep Loopminder

> 基于 LangGraph DeepAgents 的企业级多智能体协作系统

一个模拟"AI公司"运作模式的智能体框架——每个代理像公司员工一样各司其职，通过标准化的 A2A 协议相互通信协作，完成复杂任务。

---

## 核心架构：三种智能体模式

项目设计了三种不同场景的智能体架构，满足本地助手、Web端多用户、协作集群等不同需求：

| 模式 | 智能体 | 使用场景 | 核心特性 |
|------|--------|----------|----------|
| **本地助手** | `intelligent_local` | 单用户本地文件系统 | 版本化记忆 + 混合存储 + Shell执行 |
| **Web端多用户** | `intelligent_web` | 多用户网页端虚拟文件系统 | 多层级命名空间隔离 + 纯虚拟存储 |
| **协作集群** | `collaborative_agents` | AI公司模式 | 5角色分工 + A2A通信协议 |

---

## 技术亮点

### 1. 版本化记忆系统（并发安全）

所有智能体采用版本化存储机制，支持并发安全更新：

```json
{
  "version": 3,
  "current_state": {
    "user_profile": {"name": "用户名", "职业": "开发者"},
    "preferences": {"语言": "Python", "风格": "简洁"},
    "important_facts": ["用户喜欢TypeScript"],
    "active_goals": ["学习LangGraph"]
  },
  "change_history": [
    {"version": 1, "timestamp": "2025-03-20", "description": "初始化", "changes": {...}},
    {"version": 2, "timestamp": "2025-03-21", "description": "更新偏好", "changes": {...}},
    {"version": 3, "timestamp": "2025-03-22", "description": "添加目标", "changes": {...}}
  ]
}
```

**并发安全机制**：
- 每次更新必须递增 `version` 字段
- 版本冲突时需重新读取最新数据后重试
- 变更历史完整记录，支持回溯

### 2. 多层级命名空间隔离

Web端智能体实现了精细化的数据隔离：

```python
# 线程级隔离 - 每个对话独立
"/thread/" → NamespacedStoreBackend(namespace=("user_id", "thread_id"))

# 用户级共享 - 同用户跨线程共享记忆
"/user/" → NamespacedStoreBackend(namespace=("user_id", "shared_memory"))
```

**隔离效果**：
```
user_123 + thread_abc → /thread/ 独立对话数据
user_123 + thread_def → /thread/ 独立对话数据
user_123 + shared_memory → /user/ 跨线程共享用户画像
user_456 + ... → 完全隔离，无法访问 user_123 的任何数据
```

### 3. 混合存储Backend（多路由）

本地助手智能体实现文件系统与数据库的混合存储：

```python
backend=lambda rt: CompositeBackend(
    default=StateBackend(rt),                    # 默认：对话状态
    routes={
        "/workspace/": FilesystemBackend,        # 本地文件操作
        "/skills/": FilesystemBackend,           # 技能库加载
        "/user/": NamespacedStoreBackend,        # 用户记忆（数据库）
    }
)
```

**路由效果**：
- `/workspace/project.py` → 实际写入本地 `workspace/project.py`
- `/skills/frontend-design/` → 从 `skills_repo/` 加载技能
- `/user/agent.md` → 存入 PostgreSQL，命名空间隔离

### 4. 子代理委派机制

主代理可将专业任务委派给子代理处理：

```python
subagents=[
    {
        "name": "tools_Assistant",
        "description": "专业的API工具调用助手，擅长通过外部API接口获取数据...",
        "runnable": tools_Assistant  # 预编译的子代理
    }
]
```

**工作流程**：
- 主代理识别任务类型 → 委派给专业子代理
- 子代理执行API调用、数据获取等任务
- 结果返回主代理继续处理

### 5. Skills技能扩展系统

可动态加载外部技能插件，扩展智能体能力：

```python
skills=["/skills/frontend-design/", "/skills/ocr-batch"]
```

**已集成技能**：
- `frontend-design`：前端设计审美标准
- `ocr-batch`：批量OCR处理

技能通过 `SkillsMiddleware` 自动注入，智能体可在System Prompt中引用技能标准。

### 6. AI公司协作模式（5角色分工）

协作智能体集群模拟公司运作：

```
用户请求 → chat_agent (前台接待)
              ↓ 意图识别 & 智能路由
         ┌────┼────┐
         ↓    ↓    ↓
    coder  coordinator  researcher
   (工程师)   (经理)     (分析师)
```

| 智能体 | 角色 | 核心职责 |
|--------|------|----------|
| `chat_agent` | 前台接待 | 意图识别、简单对话、智能路由 |
| `coordinator_agent` | 项目经理 | 任务拆解、依赖管理、结果合成 |
| `coder_agent` | 高级工程师 | 代码编写、命令执行、调试修复 |
| `researcher_agent` | 情报分析师 | 信息检索、文档查阅、浏览器自动化 |
| `assistant_agent` | 私人秘书 | 日程管理、偏好记录、记忆维护 |

### 7. A2A（Agent-to-Agent）通信协议

标准化智能体间通信：

```python
# 咨询模式 - 向其他智能体请求意见
consult_colleague(colleague="coder_agent", question="这个API如何调用？")

# 委派模式 - 将任务完全交给其他智能体
delegate_task(colleague="researcher_agent", task="调研竞品功能", new_project=True)
```

特性：
- 每个智能体独立对话线程（UUID验证 + API注册）
- 历史对话管理与切换
- 共享记忆文件 `/memories/agent.md`

### 8. 双执行模式 & 安全隔离

| 模式 | 中间件 | 执行策略 | 安全机制 |
|------|--------|----------|----------|
| 本地 | `local_shell` | HostExecutionPolicy | workspace目录限制 |
| Web端 | `web_shell` | DockerExecutionPolicy | 容器隔离 + PII脱敏 |

**PII脱敏规则**（自动检测屏蔽敏感信息）：
```python
RedactionRule(pii_type="api_key", detector=r"sk-[a-zA-Z0-9]{20,}")
RedactionRule(pii_type="password", detector=r"password[=:]\s*\S+")
RedactionRule(pii_type="token", detector=r"token[=:]\s*\S+")
```

### 9. 模型懒加载工厂

按优先级自动选择可用模型，支持8种LLM提供商：

```python
# 优先级链
DeepSeek → OpenRouter → Ollama(本地)

# 支持的提供商
DeepSeek, GPT-4o, Gemini, 通义千问, 智谱, 豆包, OpenRouter, Ollama
```

特性：缓存机制、无API Key自动回退、模型代理类懒加载。

### 10. PostgreSQL持久化 + 自动回退

```python
async def init_postgres_checkpointer():
    try:
        global_checkpointer = AsyncPostgresSaver.from_conn_string(DB_URI)
        await global_checkpointer.setup()
    except Exception:
        global_checkpointer = InMemorySaver()  # 自动回退
```

---

## 架构图

```
┌──────────────────────────────────────────────────────────────────────┐
│                          LangGraph Server                             │
│                        http://localhost:2024                          │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │                      三种智能体模式                               │ │
│  │  ┌───────────────┐ ┌───────────────┐ ┌───────────────────────┐  │ │
│  │  │intelligent    │ │intelligent    │ │collaborative_agents  │  │ │
│  │  │   _local      │ │   _web        │ │(chat/coder/coord/...) │  │ │
│  │  │               │ │               │ │                       │  │ │
│  │  │本地文件系统   │ │虚拟文件系统   │ │ AI公司协作模式        │  │ │
│  │  │版本化记忆     │ │多层级隔离     │ │ A2A通信协议          │  │ │
│  │  │混合存储Backend│ │命名空间隔离   │ │ 共享记忆系统         │  │ │
│  │  │Shell执行      │ │子代理委派     │ │ 智能路由分发         │  │ │
│  │  │Skills技能     │ │Skills技能     │ │                       │  │ │
│  │  └───────────────┘ └───────────────┘ └───────────────────────┘  │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                                                                       │
├──────────────────────────────────────────────────────────────────────┤
│                         Middlewares Layer                             │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐        │
│  │Shell(local │ │  Logging   │ │Summarization│ │ Retry/Todo │        │
│  │   /web)    │ │  (脱敏)    │ │  (编程专用) │ │  (重试)    │        │
│  └────────────┘ └────────────┘ └────────────┘ └────────────┘        │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐                       │
│  │Skills      │ │SubAgent    │ │Memory      │                       │
│  │Middleware  │ │Middleware  │ │Middleware  │                       │
│  └────────────┘ └────────────┘ └────────────┘                       │
├──────────────────────────────────────────────────────────────────────┤
│                       CompositeBackend (混合存储)                     │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  routes:                                                        │  │
│  │    "/workspace/" → FilesystemBackend (本地文件)                 │  │
│  │    "/skills/"    → FilesystemBackend (技能库)                   │  │
│  │    "/thread/"    → NamespacedStoreBackend (user_id,thread_id)   │  │
│  │    "/user/"      → NamespacedStoreBackend (user_id,shared)      │  │
│  └────────────────────────────────────────────────────────────────┘  │
├──────────────────────────────────────────────────────────────────────┤
│                         Storage Layer                                 │
│  ┌───────────────────────────────────────────────────────────────┐   │
│  │   PostgreSQL Checkpointer/Store (失败自动回退内存存储)         │   │
│  └───────────────────────────────────────────────────────────────┘   │
│  ┌───────────────────────────────────────────────────────────────┐   │
│  │           workspace/ (虚拟根目录，沙盒隔离)                    │   │
│  │           skills_repo/ (技能扩展库)                            │   │
│  └───────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 智能体详情

### intelligent_local - 本地文件智能助手

适合单用户本地场景，拥有完整的文件系统访问能力：

```python
async def create_intelligent_deep_agent():
    return deep_custom_agent(
        # 版本化记忆系统
        memory=["/user/agent.md"],

        # 混合存储Backend
        backend=lambda rt: CompositeBackend(
            default=StateBackend(rt),
            routes={
                "/workspace/": FilesystemBackend,   # 本地文件
                "/skills/": FilesystemBackend,      # 技能库
                "/user/": NamespacedStoreBackend,   # 用户记忆
            }
        ),

        # Skills技能扩展
        skills=["/skills/frontend-design/", "/skills/ocr-batch"],

        # 子代理委派
        subagents=[{"name": "tools_Assistant", "runnable": tools_Assistant}],

        # 中间件
        middleware=[full_featured_summary, local_shell_middleware],
    )
```

### intelligent_web - 多用户虚拟文件系统

适合Web端多用户场景，纯虚拟存储 + 多层级隔离：

```python
async def create_intelligent_deep_agent_web():
    return create_deep_agent(
        # 版本化记忆系统
        memory=["/user/agent.md"],

        # 多层级命名空间隔离
        backend=lambda rt: CompositeBackend(
            default=StateBackend(rt),
            routes={
                "/thread/": NamespacedStoreBackend(namespace=("user_id", "thread_id")),
                "/user/": NamespacedStoreBackend(namespace=("user_id", "shared_memory")),
            }
        ),

        # Skills技能扩展
        skills=["skills_repo//frontend-design"],

        # 子代理委派
        subagents=[{"name": "tools_Assistant", "runnable": tools_Assistant}],
    )
```

### collaborative_agents - AI公司协作集群

5个角色分工明确的智能体，通过A2A协议协作：

| 智能体 | 存储路由 | 职责边界 |
|--------|----------|----------|
| chat_agent | 共享 `/memories/agent.md` | 自己处理闲聊/简单问答；转交复杂任务 |
| coordinator_agent | 共享 `/memories/agent.md` | 任务拆解/进度追踪；绝不处理具体代码 |
| coder_agent | 共享 `/memories/agent.md` | 代码编写/命令执行；求助业务/API问题 |
| researcher_agent | 共享 `/memories/agent.md` | 信息检索/浏览器自动化；绝不修改文件 |
| assistant_agent | 共享 `/memories/agent.md` | 日程管理/记忆维护；确认重要操作 |

---

## 中间件生态

| 中间件 | 功能 | 特性 |
|--------|------|------|
| `ShellToolMiddleware` | Shell执行 | local/web双模式，Docker隔离，PII脱敏 |
| `LoggingMiddleware` | 日志记录 | 模型/工具调用日志，敏感字段脱敏，JSON/Text双格式 |
| `SummarizationMiddleware` | 对话摘要 | 编程专用（90%+准确率），角色扮演摘要 |
| `AgentCommunicationMiddleware` | A2A通信 | consult_colleague/delegate_task工具，线程管理 |
| `SkillsMiddleware` | 技能扩展 | 动态加载技能插件，注入技能标准 |
| `SubAgentMiddleware` | 子代理委派 | 主代理委派专业任务给子代理 |
| `MemoryMiddleware` | 记忆管理 | 版本化记忆，并发安全更新 |
| `ToolRetryMiddleware` | 工具重试 | 3次重试，指数退避 |
| `TodoListMiddleware` | 任务管理 | 超过3步自动建立Todo |

---

## 快速开始

### 环境要求

- Python >= 3.12
- PostgreSQL（可选，用于持久化）
- Node.js（可选，用于 playwright-cli）

### 安装

```bash
git clone https://github.com/yachenyanyi/deep_loopminder.git
cd deep_loopminder
pip install -e .
```

### 配置 `.env`

```bash
# LLM API（至少配置一个）
DEEPSEEK_API_KEY=your_key       # 推荐
OPEN_ROUTER_API_KEY=your_key    # 多模型支持
# ... 或不配置，自动使用 Ollama 本地模型

# PostgreSQL（可选）
LANGGRAPH_POSTGRES_URI=postgresql://user:pass@localhost:5432/db
```

### 启动

```bash
langgraph dev

# API: http://localhost:2024
# Studio: https://smith.langchain.com/studio/?baseUrl=http://localhost:2024
```

---

## 使用示例

### 本地助手模式

```python
from langgraph_sdk import get_client

client = get_client(url="http://127.0.0.1:2024")
thread = await client.threads.create()

# 本地文件智能助手
response = await client.runs.wait(
    thread_id=thread["thread_id"],
    assistant_id="intelligent_local",
    input={"messages": [{"role": "user", "content": "帮我创建一个项目结构"}]}
)
```

### Web端多用户模式

```python
# 用户隔离 - 不同用户独立空间
response = await client.runs.wait(
    thread_id=thread["thread_id"],
    assistant_id="intelligent_web",
    config={"configurable": {"user_id": "user_123"}},
    input={"messages": [{"role": "user", "content": "帮我写一个Python脚本"}]}
)
```

### AI公司协作模式

```python
# 智能路由 - chat_agent自动识别并分发给专家
response = await client.runs.wait(
    thread_id=thread["thread_id"],
    assistant_id="chat_agent",
    input={"messages": [{
        "role": "user",
        "content": "帮我写一个爬虫抓取 example.com 的数据"
    }]}
)
# 流程：chat_agent → coder_agent → researcher_agent(分析网站) → coder_agent(写代码)
```

---

## 目录结构

```
deep_loopminder/
├── src/
│   ├── deep_agents/
│   │   ├── agents/
│   │   │   ├── intelligent_local.py     # 本地文件智能助手
│   │   │   ├── intelligent_web.py       # Web端多用户虚拟文件系统
│   │   │   ├── collaborative_agents.py  # AI公司5协作智能体
│   │   │   ├── role_playing.py          # 角色扮演智能体
│   │   │   └── basic_filesystem.py      # 基础文件系统代理
│   │   ├── create_custom_agents/
│   │   │   └── deep_custom_agent.py     # 自定义智能体工厂
│   │   ├── config.py                    # 全局配置
│   │   └── db.py                        # PostgreSQL + 自动回退
│   ├── middlewares/
│   │   ├── agent_communication.py       # A2A通信中间件
│   │   ├── logging.py                   # 日志中间件（脱敏）
│   │   ├── shell/
│   │   │   ├── local_shell.py           # 本地执行模式
│   │   │   └── web_shell.py             # Web端Docker隔离
│   │   ├── summarization/               # 编程专用摘要
│   │   └── execution/                   # 重试/Todo中间件
│   ├── models/
│   │   └── llm.py                       # 模型懒加载工厂（8种LLM）
│   ├── tools/                           # Shell/API工具
│   └── backend/
│       └── backend.py                   # NamespacedStoreBackend
├── workspace/                           # 智能体工作目录（虚拟根目录）
├── skills_repo/                         # Skills技能扩展库
│   └── frontend-design/                 # 前端设计技能
│   └── ocr-batch/                       # OCR批量处理技能
├── langgraph.json                       # LangGraph配置
└── README.md
```

---

## 技术栈

- **LangGraph** - 状态图框架，智能体编排
- **DeepAgents** - 深度代理框架，中间件生态
- **PostgreSQL** - 持久化存储（可选）
- **Docker** - Web端容器隔离
- **playwright-cli** - 浏览器自动化skill

---

## Roadmap

- [ ] Docker 容器隔离完整集成
- [ ] 更多Skills技能（数据分析、自动化测试）
- [ ] Web UI 界面
- [ ] 多语言支持

---

## License

MIT

---

> 🚧 本项目正在积极开发中，API 和功能可能会发生变化。