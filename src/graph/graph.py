from src.graph.agent_state import AgentState
from langgraph.graph import StateGraph,START,END
#from graph.node import Project_Analyst_Agent,Project_Manager_Agent,System_Architect_Agent,Database_Design_Agent,Bash_Agent,Task_Extraction_Agent,Loop_Coder_Agent,Code_Review_Agent,Coder_Agent,Dir_Creater_agent,User_Input_Node,Intelligent_Assistant_Agent
#from graph.node import Loop_File_Saver_Agent_0,Loop_File_Saver_Agent_1,Loop_File_Saver_Agent_2,Loop_File_Saver_Agent_3
#from src.graph.state_node import Coder_Agent_input_state

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.postgres import AsyncPostgresStore
import sqlite3
from mem0 import Memory
from mem0 import MemoryClient
from src.deep_agents.deep_agent import create_role_playing_agent
import os
import sys
import asyncio
from typing import Literal, Optional
from tavily import TavilyClient
from deepagents import create_deep_agent
from src.models.llm import default_model
from src.tools.api_tools import call_tool_tool, list_resources_tool, cleanup_mcp_client
from src.middlewares.middleware import full_featured_summary, todo_middleware
from src.agents.agent import tools_Assistant
from deepagents.backends import FilesystemBackend, StateBackend, StoreBackend, CompositeBackend
from langgraph.store.memory import InMemoryStore
from langgraph.store.base import BaseStore
from langgraph.store.postgres import AsyncPostgresStore
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
Intelligent_Deep_Assistant = create_deep_agent(
    model=default_model,
    tools=[],#call_tool_tool, list_resources_tool
    system_prompt="你是一个高级AI助手，当需要查询文档或者调用外部API或工具时，请委派给 tools_Assistant 子代理处理。",
    backend=FilesystemBackend(
        root_dir=os.path.join(os.getcwd(), "workspace"),  # 使用绝对路径
        virtual_mode=True
    ),  
    
    # middleware=[full_featured_summary, todo_middleware], # Removed to avoid duplicate middleware error as create_deep_agent adds them by default
    subagents=[
        {
            "name": "tools_Assistant", 
            "description": "专业的API工具调用助手，擅长通过外部API接口获取数据、调用服务和执行远程操作。当我需要获取实时信息、调用第三方服务、访问外部数据源或执行需要API调用的复杂任务时，应该调用此助手。它配备了call_tool和list_resources等API工具，能够处理各种需要外部接口调用的场景。",
            "runnable": tools_Assistant
        }
    ]
)

# 使用内存中的 SQLite 数据库（也可以指定文件路径）
def create_graphs_with_context():
    """创建图，不使用上下文管理器"""

    # 创建检查点（不使用 with）
    #memory_full = SqliteSaver.from_conn_string("file:graph_full.db")
    #memory_simple = SqliteSaver.from_conn_string("file:graph_simple.db")


    # 方法1：直接创建 SqliteSaver 实例

    # 构建完整图


    # 构建简单图
    simple_graph = StateGraph(AgentState)
    
    
    simple_graph.add_node("智能助手", Intelligent_Deep_Assistant)

    simple_graph.add_edge(START, "智能助手")
    simple_graph.add_edge("智能助手", END)
    

    # 编译简单图
    graph_simple = simple_graph.compile(checkpointer=InMemorySaver())

    return graph_simple