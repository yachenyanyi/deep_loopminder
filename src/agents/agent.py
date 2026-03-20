from src.models.llm import default_model
from src.backend.backend import NamespacedStoreBackend
from langchain.agents import create_agent
from deepagents import create_deep_agent
from src.tools.api_tools import call_tool_tool, list_resources_tool
from src.middlewares import full_featured_summary, role_playing_summary, mobile_action_middleware
from langchain.agents.middleware import SummarizationMiddleware
from langchain.messages import SystemMessage
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def load_prompt_from_file(filepath):
    full_path = os.path.join(BASE_DIR, filepath) if not os.path.isabs(filepath) else filepath
    with open(full_path, 'r', encoding='utf-8') as f:
        return f.read().strip()

boy_agnet = load_prompt_from_file('src/agents/boy.txt') if os.path.exists(os.path.join(BASE_DIR, 'src/agents/boy.txt')) else ""
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
