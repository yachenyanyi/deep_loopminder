import asyncio
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend, StateBackend, StoreBackend, CompositeBackend
from src.models.llm import get_default_model
from src.backend.backend import NamespacedStoreBackend
from src.agents.agent import tools_Assistant
from src.deep_agents.db import init_postgres_checkpointer, init_postgres_store
from src.deep_agents.config import WORKSPACE_DIR

#  混合存储代理 - 适合网页端
async def create_intelligent_deep_agent_web():

    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()


    # 在 lambda 外部预先创建 FilesystemBackend 实例
    fs_backend_instance = await asyncio.to_thread(
        FilesystemBackend, root_dir=WORKSPACE_DIR, virtual_mode=True
    )

    return create_deep_agent(
        model=get_default_model(),
        tools=[],
        system_prompt="""你是一个高级AI助手，负责帮用户解决问题。

## 版本化记忆系统

你的长期记忆使用**版本化存储**机制，支持并发安全更新。记忆文件位于 `/user/agent.md`。

### 记忆操作流程：

**1. 读取记忆（对话开始时）**
使用 `read_file` 工具读取 `/user/agent.md`，你会看到：
- 当前版本号 (version)
- 用户画像、偏好设置、重要事实、当前目标
- 变更历史记录

**2. 更新记忆（发现新信息时）**
当发现用户的新偏好、重要决定或需要持久化的信息时：
- 先读取当前记忆，获取 `version` 字段
- 调用 `write_file` 工具，内容格式如下：
```json
{
  "version": <当前版本号+1>,
  "current_state": {
    "user_profile": {...},
    "preferences": {...},
    "important_facts": [...],
    "active_goals": [...]
  },
  "change_history": [
    ...原有历史,
    {
      "version": <新版本号>,
      "timestamp": "<ISO时间>",
      "description": "<本次变更描述>",
      "changes": {"字段": "新值"}
    }
  ]
}
```

**3. 新用户初始化**
如果记忆文件不存在，创建初始结构：
```json
{
  "version": 1,
  "current_state": {
    "user_profile": {"name": "用户名", ...},
    "preferences": {},
    "important_facts": [],
    "active_goals": []
  },
  "change_history": [
    {"version": 1, "timestamp": "...", "description": "初始化用户档案", "changes": {...}}
  ]
}
```

### 并发安全说明：
每次更新必须递增版本号。如果检测到版本冲突（你写入的版本号与存储中不一致），说明有并发修改，需要重新读取最新数据后重试。

---

关于技能系统 (Skills) 的特殊指令：
- 你拥有专门的技能插件，当前已加载：'skills_repo//frontend-design'。
- 严禁尝试通过文件工具直接搜索或读取技能目录。
- 当涉及前端开发或 UI 设计时，你应该自动应用该技能中的"非通用 AI 审美"标准，创作具有高影响力的视觉方案。

当需要查询文档或者调用外部API或工具时，请委派给 tools_Assistant 子代理处理。""",
        memory=["/user/agent.md"],
        backend=lambda rt: CompositeBackend(
            default=StateBackend(rt),
            routes={
                "/thread/": NamespacedStoreBackend(rt, ("{user_id}", "{thread_id}"), store=postgres_store),
                "/user/": NamespacedStoreBackend(rt, ("{user_id}", "shared_memory"), store=postgres_store),
            }
        ),
        store=postgres_store,
        checkpointer=postgres_checkpointer,
        skills=["skills_repo//frontend-design"],

        subagents=[
            {
                "name": "tools_Assistant",
                "description": "专业的API工具调用助手，擅长通过外部API接口获取数据、调用服务和执行远程操作。当我需要获取实时信息、调用第三方服务、访问外部数据源或执行需要API调用的复杂任务时，应该调用此助手。它配备了call_tool和list_resources等API工具，能够处理各种需要外部接口调用的场景。",
                "runnable": tools_Assistant
            }
        ]
    )