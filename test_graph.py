
from src.graph.graph import create_graphs_with_context
graph_simple = create_graphs_with_context()
config = {"configurable": {"thread_id": "main-chat"}}
for chunk in graph_simple.stream(
    {"input": "你是谁"},
    config,
    stream_mode="values"
):
    # 从chunk中提取信息
    if "messages" in chunk:
        latest_msg = chunk["messages"][-1]
        if hasattr(latest_msg, 'content'):
            print(f"输出: {latest_msg.content}")