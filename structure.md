langgraph-project/
│
├── 📁 src/                           # 源代码目录
│   ├── 📁 agents/                    # 代理定义
│   │   ├── __init__.py
│   │   ├── base_agent.py            # 基础代理类
│   │   ├── chatbot_agent.py         # 聊天机器人代理
│   │   ├── rag_agent.py             # RAG检索代理
│   │   ├── deep_agent.py            # 🔥 Deep Agent实现
│   │   └── workflow_agent.py        # 工作流代理
│   │
│   ├── 📁 graphs/                    # LangGraph图定义
│   │   ├── __init__.py
│   │   ├── chatbot_graph.py          # 聊天机器人图
│   │   ├── rag_graph.py              # RAG图
│   │   ├── deep_graph.py             # 🔥 Deep Agent图
│   │   ├── state_schemas.py          # 状态模式定义
│   │   └── edges.py                  # 边和条件逻辑
│   │
│   ├── 📁 nodes/                     # 图节点定义
│   │   ├── __init__.py
│   │   ├── llm_nodes.py              # LLM调用节点
│   │   ├── tool_nodes.py             # 工具执行节点
│   │   ├── memory_nodes.py           # 记忆管理节点
│   │   ├── deep_nodes.py             # 🔥 Deep Agent专用节点
│   │   └── utility_nodes.py          # 工具节点
│   │
│   ├── 📁 tools/                     # 工具定义
│   │   ├── __init__.py
│   │   ├── calculator.py             # 计算器工具
│   │   ├── search.py                 # 搜索工具
│   │   ├── database.py               # 数据库工具
│   │   ├── file_tools.py             # 🔥 文件系统工具
│   │   └── custom_tools.py           # 自定义工具
│   │
│   ├── 📁 deep_agents/               # 🔥 Deep Agents专用模块
│   │   ├── __init__.py
│   │   ├── deep_agent_config.py      # Deep Agent配置
│   │   ├── middleware/               # 中间件配置
│   │   │   ├── __init__.py
│   │   │   ├── filesystem_config.py  # 文件系统中间件
│   │   │   ├── todo_middleware.py   # 待办事项中间件
│   │   │   └── subagent_config.py   # 子代理中间件
│   │   ├── prompts/                  # 系统提示词
│   │   │   ├── __init__.py
│   │   │   ├── researcher_prompt.py # 研究员提示词
│   │   │   ├── developer_prompt.py  # 开发者提示词
│   │   │   └── analyst_prompt.py    # 分析师提示词
│   │   └── memory/                   # Deep Agent记忆管理
│   │       ├── __init__.py
│   │       ├── memory_store.py       # 记忆存储
│   │       └── memory_protocols.py   # 记忆协议
│   │
│   ├── 📁 models/                    # 模型配置
│   │   ├── __init__.py
│   │   ├── llm_config.py             # LLM配置
│   │   ├── embeddings.py              # 嵌入模型
│   │   └── providers.py               # 模型提供商
│   │
│   ├── 📁 memory/                    # 记忆管理
│   │   ├── __init__.py
│   │   ├── short_term_memory.py      # 短期记忆
│   │   ├── long_term_memory.py       # 长期记忆
│   │   └── vector_store.py           # 向量存储
│   │
│   ├── 📁 state/                     # 状态管理
│   │   ├── __init__.py
│   │   ├── state_models.py           # 状态模型
│   │   ├── persistence.py            # 持久化
│   │   └── checkpoints.py            # 检查点
│   │
│   ├── 📁 utils/                     # 工具函数
│   │   ├── __init__.py
│   │   ├── logger.py                 # 日志配置
│   │   ├── validators.py             # 验证器
│   │   └── helpers.py                # 辅助函数
│   │
│   └── 📁 config/                    # 配置管理
│       ├── __init__.py
│       ├── settings.py               # 应用设置
│       ├── prompts.py                # 提示词模板
│       └── constants.py              # 常量定义
│
├── 📁 api/                           # API接口
│   ├── __init__.py
│   ├── main.py                       # FastAPI主应用
│   ├── routes/                       # 路由定义
│   │   ├── __init__.py
│   │   ├── agent_routes.py         # 代理相关路由
│   │   ├── deep_agent_routes.py    # 🔥 Deep Agent路由
│   │   └── health_routes.py        # 健康检查
│   └── middleware/                  # 中间件
│       ├── __init__.py
│       ├── cors.py                  # CORS配置
│       └── error_handler.py         # 错误处理
│
├── 📁 tests/                         # 测试目录
│   ├── __init__.py
│   ├── unit/                        # 单元测试
│   │   ├── test_agents.py
│   │   ├── test_graphs.py
│   │   ├── test_deep_agents.py     # 🔥 Deep Agent测试
│   │   └── test_tools.py
│   ├── integration/                 # 集成测试
│   │   ├── test_workflows.py
│   │   ├── test_deep_workflows.py  # 🔥 Deep Agent工作流测试
│   │   └── test_api.py
│   └── conftest.py                  # 测试配置
│
├── 📁 examples/                      # 示例代码
│   ├── simple_chatbot.py            # 简单聊天机器人
│   ├── rag_system.py                 # RAG系统示例
│   ├── deep_research_agent.py       # 🔥 Deep Research代理
│   ├── multi_agent_system.py         # 多代理系统
│   ├── complex_workflow.py           # 复杂工作流
│   └── custom_deep_agent.py         # 🔥 自定义Deep Agent
│
├── 📁 deployment/                    # 🔥 部署配置
│   ├── docker/
│   │   ├── Dockerfile.deepagent     # Deep Agent专用Docker
│   │   └── docker-compose.deep.yml  # Deep Agent Compose
│   ├── kubernetes/
│   │   ├── deep-agent-deployment.yaml
│   │   └── deep-agent-configmap.yaml
│   └── configs/
│       ├── deep_agent_config.json   # Deep Agent配置
│       └── middleware_config.yaml   # 中间件配置
│
├── 📁 docs/                          # 文档
│   ├── README.md                     # 项目说明
│   ├── API.md                        # API文档
│   ├── ARCHITECTURE.md               # 架构设计
│   ├── DEEP_AGENTS.md               # 🔥 Deep Agents专用文档
│   └── tutorials/                    # 教程文档
│       ├── quickstart.md
│       ├── deep_agents_guide.md     # 🔥 Deep Agents指南
│       └── advanced_usage.md
│
├── 📁 scripts/                       # 脚本文件
│   ├── setup_env.py                  # 环境设置
│   ├── setup_deep_agents.py         # 🔥 Deep Agents环境设置
│   ├── run_dev.py                    # 开发模式运行
│   ├── run_deep_agent.py            # 🔥 运行Deep Agent
│   └── run_tests.py                  # 测试运行
│
├── 📁 data/                          # 数据目录
│   ├── raw/                          # 原始数据
│   ├── processed/                    # 处理后的数据
│   ├── vector_db/                    # 向量数据库
│   └── deep_agent_memories/         # 🔥 Deep Agent记忆存储
│
├── 📁 logs/                          # 日志文件
│   ├── langgraph_logs/              # LangGraph日志
│   └── deep_agent_logs/             # 🔥 Deep Agent日志
│
├── 📄 .env.example                   # 环境变量示例
├── 📄 .gitignore                     # Git忽略文件
├── 📄 requirements.txt               # Python依赖
├── 📄 requirements-deep.txt         # 🔥 Deep Agents专用依赖
├── 📄 pyproject.toml                 # 项目配置
├── 📄 Dockerfile                     # Docker配置
├── 📄 Dockerfile.deepagent          # 🔥 Deep Agent Docker
├── 📄 docker-compose.yml             # Docker Compose
├── 📄 docker-compose.deep.yml       # 🔥 Deep Agents Compose
└── 📄 README.md                      # 项目说明