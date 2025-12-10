from langchain.agents.middleware import SummarizationMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware
from src.models.llm import default_model
#from src.tools.mcp_tools import mcp_tools
from langchain.agents.middleware import ToolRetryMiddleware
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware import LLMToolSelectorMiddleware


full_featured_summary = SummarizationMiddleware(
    model=default_model,
    trigger=("tokens", 20000),      # 使用元组格式：(kind, value)
    keep=("messages", 10),          # 使用元组格式：(kind, value)
    summary_prompt="重点总结以下对话中的:\n1. 用户需求\n2. 已解决的问题\n3. 关键决定\n\n对话内容:\n{messages}",
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