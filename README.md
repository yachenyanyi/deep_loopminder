# 智能深度助手 (Intelligent Deep Assistant)

一个基于LangChain构建的智能AI助手，支持多种存储后端和专业化配置，能够安全地处理文件操作、API调用和复杂任务。

## 🚀 功能特性

- **多后端支持**: 文件系统、状态存储、持久化记忆、混合存储等多种后端
- **安全文件操作**: 沙盒环境下的文件读写操作
- **API工具集成**: 支持外部API调用和数据获取
- **专业化代理**: 针对不同使用场景优化的代理配置
- **异步处理**: 全异步架构，支持流式响应
- **子代理委派**: 智能任务分配给专业子代理

## 📁 项目结构

```
re_build/
├── src/
│   ├── agents/           # 基础代理定义
│   ├── deep_agents/      # 深度代理配置和实现
│   ├── middlewares/      # 中间件功能
│   ├── models/           # LLM模型配置
│   ├── tools/            # API工具集
│   └── utils/            # 工具函数
├── workspace/            # 工作空间目录
├── main.py              # 主程序入口
└── mock_tools_test.py   # 测试文件
```

## 🛠️ 安装和配置

### 环境要求
- Python 3.12+
- DeepSeek API密钥

### 快速开始

1. **克隆项目**
   ```bash
   git clone <your-repo-url>
   cd re_build
   ```

2. **安装依赖**
   ```bash
   pip install langchain langchain-core langgraph deepagents tavily
   ```

3. **配置API密钥**
   在`main.py`中设置您的DeepSeek API密钥：
   ```python
   os.environ["DEEPSEEK_API_KEY"] = "your-api-key-here"
   ```

4. **运行程序**
   ```bash
   python main.py
   ```

## 🎯 使用场景

### 1. 基础文件系统代理
适合安全的本地文件操作，如文档管理、代码编辑等。
```python
from src.deep_agents.deep_agent import Basic_Filesystem_Agent

# 使用基础文件系统代理
agent = Basic_Filesystem_Agent
```

### 2. 临时状态代理
适合会话级别的临时任务，数据分析、草稿编写等。
```python
from src.deep_agents.deep_agent import State_Only_Agent

# 使用临时状态代理
agent = State_Only_Agent
```

### 3. 持久化记忆代理
适合需要长期记忆的场景，项目管理、学习记录等。
```python
from src.deep_agents.deep_agent import Persistent_Memory_Agent

# 使用持久化记忆代理
agent = Persistent_Memory_Agent
```

### 4. 混合存储代理
结合本地文件和云端存储，适合复杂的企业应用。
```python
from src.deep_agents.deep_agent import Hybrid_Storage_Agent

# 使用混合存储代理
agent = Hybrid_Storage_Agent
```

### 5. 智能深度助手（默认）
综合性的智能助手，适合大多数通用场景。
```python
from src.deep_agents.deep_agent import Intelligent_Deep_Assistant

# 使用默认的智能深度助手
agent = Intelligent_Deep_Assistant
```

## 🔧 代理配置详解

### 存储后端类型

1. **FilesystemBackend** - 本地文件系统存储
   - `root_dir`: 根目录路径
   - `virtual_mode`: 是否启用沙盒模式

2. **StateBackend** - 临时状态存储
   - 会话级别存储，重启后数据丢失

3. **StoreBackend** - 持久化存储
   - 跨会话持久化数据
   - 支持多种存储后端

4. **CompositeBackend** - 混合存储
   - 路径路由到不同后端
   - 灵活的配置选项

### 子代理系统

每个深度代理都可以配置子代理，用于处理特定类型的任务：

- **tools_Assistant**: 专业的API工具调用助手
- **research_Assistant**: 研究分析助手
- **file_Assistant**: 文件操作助手

## 💡 使用示例

### 基本对话
```python
import asyncio
from langchain_core.messages import HumanMessage
from src.deep_agents.deep_agent import Intelligent_Deep_Assistant

async def chat():
    messages = [HumanMessage(content="你好，请介绍一下自己")]
    
    async for event in Intelligent_Deep_Assistant.astream_events({"messages": messages}):
        if event["event"] == "on_chat_model_stream":
            print(event["data"]["chunk"].content, end="", flush=True)

asyncio.run(chat())
```

### 文件操作
```python
# 创建文件
agent = Basic_Filesystem_Agent
result = await agent.ainvoke({
    "messages": [HumanMessage(content="请在workspace目录下创建一个test.txt文件，内容为'Hello World'")]
})
```

### API调用
```python
# 通过子代理调用API
result = await Intelligent_Deep_Assistant.ainvoke({
    "messages": [HumanMessage(content"请帮我查询最新的天气信息")]
})
```

## 🔒 安全特性

- **沙盒文件系统**: 限制文件访问范围，防止越权访问
- **API密钥保护**: 环境变量存储敏感信息
- **异步安全**: 防止阻塞和资源泄露
- **错误处理**: 完善的异常处理机制

## 🚀 高级功能

### 自定义代理创建
```python
from deepagents import create_deep_agent
from src.models.llm import default_model

# 创建自定义代理
my_agent = create_deep_agent(
    model=default_model,
    tools=[my_custom_tool],
    system_prompt="你的系统提示词",
    backend=FilesystemBackend(root_dir="./my_workspace"),
    subagents=[
        {
            "name": "my_sub_agent",
            "description": "子代理描述",
            "runnable": my_sub_agent
        }
    ]
)
```

### 中间件使用
```python
from src.middlewares.middleware import full_featured_summary, todo_middleware

# 应用中间件
agent = create_deep_agent(
    # ... 其他配置
    middleware=[full_featured_summary, todo_middleware]
)
```

## 📊 性能优化

- **异步处理**: 所有操作都是异步的，支持高并发
- **流式响应**: 支持实时流式输出
- **内存管理**: 智能的内存使用和清理
- **缓存机制**: 支持结果缓存，提高响应速度

## 🔧 故障排除

### 常见问题

1. **API密钥错误**
   - 检查DEEPSEEK_API_KEY是否正确设置
   - 确认API密钥有效且未过期

2. **文件权限问题**
   - 检查workspace目录权限
   - 确认沙盒配置正确

3. **依赖问题**
   - 确保所有依赖包已正确安装
   - 检查Python版本兼容性

### 调试模式
```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

## 🤝 贡献指南

1. Fork项目到您的仓库
2. 创建功能分支：`git checkout -b feature/新功能`
3. 提交更改：`git commit -m '添加新功能'`
4. 推送到分支：`git push origin feature/新功能`
5. 创建Pull Request

## 📄 许可证

本项目基于MIT许可证开源 - 查看[LICENSE](LICENSE)文件了解详情。

## 🙏 致谢

- [LangChain](https://langchain.com/) - 强大的LLM应用框架
- [DeepSeek](https://deepseek.com/) - 优秀的AI模型
- [Tavily](https://tavily.com/) - 搜索API服务

## 📞 联系方式

如有问题或建议，请通过以下方式联系：
- 创建Issue
- 提交Pull Request
- 邮件联系

---

**享受您的智能助手开发之旅！** 🎉