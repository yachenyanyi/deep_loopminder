# LangGraph Deep Agent System

一个基于LangGraph的智能代理系统，提供多种存储后端和专业化代理配置，支持从临时数据处理到企业级应用的完整场景覆盖。

## 🚀 项目概述

本项目构建了一个模块化的AI代理系统，通过LangGraph框架实现状态化的多代理应用。系统提供8种不同配置的代理类型，每种都针对特定的使用场景进行了优化，从高性能内存处理到企业级持久化存储，满足不同复杂度的业务需求。

### 核心特性

- **多存储后端支持**: StateBackend、FilesystemBackend、StoreBackend、CompositeBackend
- **智能路由系统**: 基于路径前缀的自动存储后端选择
- **企业级安全**: 沙盒化文件访问、审计日志、权限控制
- **高性能处理**: 内存优化、异步处理、连接池管理
- **持久化存储**: PostgreSQL支持、跨会话记忆、数据备份
- **角色扮演能力**: 长对话记忆、性格一致性、情感智能

## 📋 代理类型

### 1. Basic_Filesystem_Agent - 基础文件系统代理
**适用场景**: 安全的本地文件操作
- 沙盒化文件访问 (`virtual_mode=True`)
- 文档管理、代码编辑、配置文件维护
- 工作目录: `./workspace/`

### 2. State_Only_Agent - 临时状态代理
**适用场景**: 会话级别的临时数据处理
- 纯内存存储，最佳性能
- 临时数据分析、草稿编写、快速原型
- 会话结束后数据自动清理

### 3. Persistent_Memory_Agent - 持久化存储代理
**适用场景**: 跨会话的长期记忆
- 基于PostgreSQL的持久化存储
- 个人知识管理、项目跟踪、长期学习记录
- 支持命名空间隔离

### 4. Hybrid_Storage_Agent - 混合存储代理
**适用场景**: 智能路由不同存储后端
- `/tmp/` → 临时存储 (StateBackend)
- `/memories/` → 永久存储 (StoreBackend)
- `/workspace/` → 本地文件 (FilesystemBackend)

### 5. Analytics_Agent - 高性能分析代理
**适用场景**: 大数据处理和分析
- 内存优化，无磁盘I/O开销
- CSV处理、JSON分析、日志分析、统计计算
- 适合复杂数据转换和实时分析

### 6. Enterprise_Agent - 企业级代理
**适用场景**: 生产环境和企业应用
- `/documents/` → 企业文档管理
- `/audit/` → 审计日志记录
- `/config/` → 配置管理
- 符合合规性和安全要求

### 7. Role_Playing_Agent - 角色扮演代理
**适用场景**: 长对话和角色扮演
- 性格一致性和情感记忆
- 线程级别的对话记忆 (PostgreSQL)
- 智能记忆检索和token优化

### 8. Intelligent_Deep_Assistant - 智能深度助手
**适用场景**: 综合智能助手（默认配置）
- 向后兼容原有配置
- 基础文件系统 + API工具调用

## 🛠️ 技术架构

### 存储后端对比

| 后端类型 | 持久性 | 性能 | 主要用途 |
|---------|--------|------|----------|
| StateBackend | 会话级别 | ★★★★★ | 临时数据处理 |
| FilesystemBackend | 本地磁盘 | ★★★☆☆ | 文件系统操作 |
| StoreBackend | 跨会话 | ★★★★☆ | 长期记忆存储 |
| CompositeBackend | 混合 | 可变 | 复杂路由需求 |

### 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    Deep Agent System                       │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐        │
│  │   Agents    │ │ Middleware  │ │    Tools    │        │
│  │  (8 Types)  │ │  (Summary,  │ │  (API, File)│        │
│  │             │ │   Todo,     │ │             │        │
│  │             │ │  Role Play) │ │             │        │
│  └──────┬──────┘ └──────┬──────┘ └──────┬──────┘        │
│         │              │              │                 │
│  ┌──────┴──────────────┴──────────────┴──────┐        │
│  │            Storage Backends                 │        │
│  │  ┌──────────┬──────────┬──────────┐       │        │
│  │  │  State   │ FileSys  │  Store   │       │        │
│  │  │ Backend  │ Backend  │ Backend  │       │        │
│  │  └──────────┴──────────┴──────────┘       │        │
│  │  ┌────────────────────────────────────┐   │        │
│  │  │      Composite Backend             │   │        │
│  │  │  (Path-based Routing)              │   │        │
│  │  └────────────────────────────────────┘   │        │
│  └──────────────────────────────────────────────┘        │
│                                                           │
│  ┌─────────────────────────────────────────────────────┐  │
│  │              PostgreSQL Database                   │  │
│  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ │  │
│  │  │ Checkpointer│ │    Store    │ │    Audit    │ │  │
│  │  │  (Threads)  │ │  (Memories) │ │    Logs     │ │  │
│  │  └─────────────┘ └─────────────┘ └─────────────┘ │  │
│  └─────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## 🔧 快速开始

### 环境要求

- Python >= 3.11
- PostgreSQL (可选，用于持久化存储)
- 支持的LLM API密钥 (OpenAI, Anthropic等)

### 安装依赖

```bash
# 克隆项目
git clone <repository-url>
cd re_build

# 安装基础依赖
pip install -e .

# 安装开发依赖（包含langgraph-cli）
pip install -e ".[dev]"

# 或者使用uv（推荐）
uv sync --group dev
```

### 配置环境

创建 `.env` 文件：

```bash
# LLM API配置
OPENAI_API_KEY=your_openai_api_key
ANTHROPIC_API_KEY=your_anthropic_api_key

# PostgreSQL配置（可选）
POSTGRES_DB_URI=postgresql://user:password@localhost:5432/database

# 其他配置
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=your_langchain_api_key
```

### 启动开发服务器

```bash
# 启动LangGraph开发服务器
langgraph dev

# 服务器将在 http://localhost:2024 启动
# 访问 Studio UI: https://smith.langchain.com/studio/?baseUrl=http://localhost:2024
```

### 使用代理

```python
from src.deep_agents import create_intelligent_deep_agent

# 获取本地端智能代理
agent = await create_intelligent_deep_agent()

# 或使用网页端版本
from src.deep_agents import create_intelligent_deep_agent_web
agent_web = await create_intelligent_deep_agent_web()

# 其他可用代理
from src.deep_agents import (
    create_role_playing_agent,
    create_basic_filesystem_agent,
    create_state_only_agent,
    create_persistent_memory_agent,
    create_analytics_agent,
    create_enterprise_agent,
)
```

## 📊 使用示例

### 1. 数据分析任务

```python
# 使用Analytics代理处理CSV数据
from src.deep_agents import create_analytics_agent
agent = await create_analytics_agent()
result = await agent.ainvoke({
    "messages": ["请分析这个CSV文件中的销售数据，找出趋势和异常"],
    "file_path": "sales_data.csv"
})
```

### 2. 长期知识管理

```python
# 使用Persistent_Memory代理构建知识库
from src.deep_agents import create_persistent_memory_agent
agent = await create_persistent_memory_agent()
result = await agent.ainvoke({
    "messages": ["记住这个重要的项目信息..."]
})

# 后续会话中可以检索记忆
result = await agent.ainvoke({
    "messages": ["我之前提到的项目信息是什么？"]
})
```

### 3. 企业文档管理

```python
# 使用Enterprise代理管理企业文档
from src.deep_agents import create_enterprise_agent
agent = await create_enterprise_agent()
result = await agent.ainvoke({
    "messages": ["创建项目报告并保存到/documents/目录"]
})
```

### 4. 角色扮演对话

```python
# 使用Role_Playing代理进行长对话
from src.deep_agents import create_role_playing_agent
agent = await create_role_playing_agent()
result = await agent.ainvoke({
    "messages": ["从现在开始，你是一个经验丰富的编程导师..."],
    "character": "programming_mentor"
})
```

## 🔧 高级配置

### 自定义存储路由

```python
from deepagents.backends import CompositeBackend
from deepagents.backends import StateBackend, StoreBackend, FilesystemBackend

# 创建自定义混合存储配置
backend = CompositeBackend(
    default=StateBackend(),
    routes={
        "/cache/": StateBackend(),      # 缓存数据
        "/knowledge/": StoreBackend(), # 知识库
        "/files/": FilesystemBackend()   # 文件存储
    }
)
```

### PostgreSQL连接池

```python
# 系统使用全局连接池管理PostgreSQL连接
# 在deep_agent.py中配置连接参数
DB_URI = 'postgresql://user:password@localhost:5432/database'

# 连接池特性
- 自动重连和故障恢复
- 连接池管理和优化
- 异步上下文管理
```

### 中间件配置

```python
# 系统支持多种中间件
middleware = [
    full_featured_summary,    # 智能摘要
    todo_middleware,          # 任务管理
    role_playing_summary      # 角色扮演摘要
]
```

## 🧪 测试

### 运行测试

```bash
# 运行单元测试
pytest tests/unit_tests/

# 运行集成测试
pytest tests/integration_tests/

# 运行所有测试
make test
```

### 测试覆盖

- **单元测试**: 核心功能、存储后端、工具函数
- **集成测试**: 代理工作流、数据库集成、API调用
- **性能测试**: 内存使用、响应时间、并发处理

## 📈 性能优化

### 内存管理

- **智能摘要**: 自动压缩长对话历史
- **选择性记忆**: 优先记住重要信息，过滤无关细节
- **渐进式加载**: 根据对话需要动态加载相关记忆

### Token优化

- **上下文管理**: 智能维护对话上下文
- **消息压缩**: 自动压缩历史消息
- **优先级排序**: 根据重要性排序记忆内容

### 数据库优化

- **连接池**: 复用数据库连接
- **索引优化**: 为常用查询创建索引
- **异步操作**: 非阻塞数据库操作

## 🔒 安全特性

### 文件系统安全

- **沙盒模式**: `virtual_mode=True` 限制文件访问
- **路径验证**: 防止路径遍历攻击
- **权限控制**: 细粒度的文件访问权限

### 数据安全

- **连接加密**: PostgreSQL SSL连接支持
- **敏感数据**: 环境变量管理API密钥
- **审计日志**: 记录重要操作和访问

### 运行时安全

- **输入验证**: 所有用户输入都经过验证
- **错误处理**: 安全的错误信息和异常处理
- **资源限制**: 防止资源滥用和内存泄漏

## 🔍 监控和调试

### 日志配置

```python
import logging

# 配置日志级别
logging.basicConfig(level=logging.INFO)

# 模块特定日志
logger = logging.getLogger(__name__)
```

### 性能监控

- **响应时间**: 跟踪代理响应时间
- **内存使用**: 监控内存使用情况
- **数据库性能**: 监控查询性能和连接状态

### 调试工具

```bash
# 使用LangGraph Studio进行可视化调试
langgraph dev --debug

# 查看代理状态
langgraph inspect
```

## 📚 相关文档

- [LangGraph 文档](https://python.langchain.com/docs/langgraph/)
- [LangChain 文档](https://python.langchain.com/docs/)
- [Deep Agent 类型说明](./src/deep_agents/AGENT_TYPES.md)
- [API 参考](./openapi.json)

## 🤝 贡献

1. Fork 项目
2. 创建特性分支 (`git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'Add some amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 创建 Pull Request

## 📝 许可证

本项目采用 MIT 许可证 - 查看 [LICENSE](LICENSE) 文件了解详情。

## 🙏 致谢

- [LangChain](https://langchain.com/) - 强大的LLM应用框架
- [LangGraph](https://langchain.com/langgraph) - 状态化多代理系统
- [deepagents](https://github.com/langchain-ai/deepagents) - 深度代理框架

## 📞 支持

如有问题或建议，请通过以下方式联系：

- 创建 GitHub Issue
- 查看项目文档
- 参考示例代码

---

**注意**: 本项目正在积极开发中，API和功能可能会发生变化。请关注版本更新和变更日志。