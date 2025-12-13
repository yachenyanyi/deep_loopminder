from typing import TypedDict, Annotated, List
from langgraph.graph import StateGraph, END, START
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage
from src.agents.agent import Intelligent_Assistant
from src.deep_agents.deep_agent import init_postgres_checkpointer

# 定义状态
class GraphState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]

# 定义节点函数
async def intelligent_node(state: GraphState):
    """主智能助手的处理节点"""
    messages = state["messages"]
    # 调用 Intelligent_Assistant
    response = await Intelligent_Assistant.ainvoke({"messages": messages})
    return {"messages": response["messages"]}

# 创建简单图的工厂函数
async def create_simple_graph():
    """创建一个带有Postgres持久化的简单图"""
    # 构建图
    workflow = StateGraph(GraphState)

    # 添加节点
    workflow.add_node("intelligent_assistant", intelligent_node)

    # 添加边
    workflow.add_edge(START, "intelligent_assistant")
    workflow.add_edge("intelligent_assistant", END)

    # 初始化 checkpointer
    checkpointer = await init_postgres_checkpointer()

    # 编译图
    app = workflow.compile(checkpointer=checkpointer)
    return app
