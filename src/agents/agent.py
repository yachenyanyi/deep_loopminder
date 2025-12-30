from src.models.llm import default_model
from src.backend.backend import NamespacedStoreBackend
from langchain.agents import create_agent
from deepagents import create_deep_agent
from src.tools.api_tools import call_tool_tool, list_resources_tool
from src.middlewares.middleware import full_featured_summary,role_playing_summary
def load_prompt_from_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        return f.read().strip()  # .strip() 移除首尾空白字符

boy_agnet=load_prompt_from_file('src/agents/boy.txt')
tools_Assistant = create_agent(

    model=default_model,
    tools=[call_tool_tool, list_resources_tool],
    system_prompt="你是我的工具助手，我可以调用工具来完成任务。",
    name="tools_Assistant",
    middleware=[]
)
Intelligent_Assistant = create_agent(
    model=default_model,
    tools=[],
    system_prompt=boy_agnet,
    
    
    name="Intelligent_Assistant",
    middleware=[role_playing_summary]
)