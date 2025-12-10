from src.models.llm import default_model

from langchain.agents import create_agent
from deepagents import create_deep_agent
from src.tools.api_tools import call_tool_tool, list_resources_tool
tools_Assistant = create_agent(

    model=default_model,
    tools=[call_tool_tool, list_resources_tool],
    system_prompt="你是我的工具助手，我可以调用工具来完成任务。",
    name="tools_Assistant",
    middleware=[]
)
