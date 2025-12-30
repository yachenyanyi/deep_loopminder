from langchain.agents.middleware import SummarizationMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware
from src.models.llm import default_model
#from src.tools.mcp_tools import mcp_tools
from langchain.agents.middleware import ToolRetryMiddleware
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware import LLMToolSelectorMiddleware


full_featured_summary = SummarizationMiddleware(
    model=default_model,
    trigger=("tokens", 10000),      # 使用元组格式：(kind, value)
    keep=("messages", 10),          # 使用元组格式：(kind, value)
    summary_prompt=(
        "你被委派负责压缩一个编程对话上下文。这对维持编程代理的高效工作记忆至关重要。\n\n"
        "**压缩优先级 (按顺序):**\n"
        "1. **当前任务状态**: 正在进行的任务是什么\n"
        "2. **错误与解决方案**: 所有遇到的错误及其解决方法\n"
        "3. **代码演进**: 仅保留最终可行版本 (移除中间尝试)\n"
        "4. **系统上下文**: 项目结构、依赖关系、环境配置\n"
        "5. **设计决策**: 架构选择及其理由\n"
        "6. **待办事项**: 未完成的任务和已知问题\n\n"
        "**压缩规则:**\n"
        "- **必须保留**: 错误信息、堆栈跟踪、有效的解决方案、当前任务\n"
        "- **合并**: 将相似的讨论合并为单一摘要点\n"
        "- **移除**: 冗余解释、失败尝试 (保留经验教训)、冗长的评论\n"
        "- **浓缩**: 长代码块 → 仅保留签名 + 关键逻辑\n\n"
        "**特殊处理:**\n"
        "- 代码: < 20 行保留全文，否则保留签名 + 关键逻辑\n"
        "- 错误: 保留完整错误信息 + 最终解决方案\n"
        "- 讨论: 仅提取决策和行动项\n\n"
        "**输入上下文:**\n"
        "{messages}\n\n"
        "**必需的输出结构:**\n\n"
        "<current_focus>\n"
        "[我们现在正在做什么]\n"
        "</current_focus>\n\n"
        "<environment>\n"
        "- [关键设置/配置点]\n"
        "</environment>\n\n"
        "<completed_tasks>\n"
        "- [任务]: [简要结果]\n"
        "</completed_tasks>\n\n"
        "<active_issues>\n"
        "- [问题]: [状态/下一步]\n"
        "</active_issues>\n\n"
        "<code_state>\n"
        "  <file path='filename'>\n"
        "    **Summary**: [该文件的作用]\n"
        "    **Key elements**: [重要函数/类]\n"
        "    **Latest version**: [该文件中的关键代码片段]\n"
        "  </file>\n"
        "  ...更多文件...\n"
        "</code_state>\n\n"
        "<important_context>\n"
        "- [上述未涵盖的任何关键信息]\n"
        "</important_context>"
    ),
)


role_playing_summary = SummarizationMiddleware(
    model=default_model,
    trigger=("tokens", 30000),
    keep=("messages", 30),
    summary_prompt=(
        """你是一位小说作者，你需要为这段故事写一段总结章节，保证读者可以清楚了解前文时间线，剧情发展，当前事件，出场人物"""
    ),
)




retry_middleware = ToolRetryMiddleware(
    max_retries=3,          # 最多重试3次
    initial_delay=1.0,      # 第一次重试前等1秒
    backoff_factor=2.0,     # 每次等待时间翻倍
    # tools=["call_tool"]   # 可选：只针对特定工具开启
)
todo_middleware = TodoListMiddleware(
    # 自定义提示词，告诉 Agent 什么时候该用 Todo
    system_prompt=(
        "你是一个严谨的项目经理，遇到超过3步的任务必须建立 Todo List。\n"
        "请使用 SetTodoList 工具来管理你的任务清单。\n"
        "每完成一个步骤，请更新 Todo List 的状态。"
    )
)