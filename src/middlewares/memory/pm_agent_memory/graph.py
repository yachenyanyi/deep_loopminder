"""后台记忆图入口：完整的任务执行功能集中在此文件。"""

from langgraph.graph import END, START, StateGraph

from .jobs import MemoryInput, PMMemory
from .provider import get_pm_memory_store


async def pm_memory_agent():
    """创建后台记忆图，统一处理来源快照和官方定期整理任务。"""
    from src.models.llm import get_default_model

    model = get_default_model()
    memory = PMMemory((await get_pm_memory_store()).store, model)

    async def remember(state: MemoryInput):
        """读取固定来源或采集整理快照，再交给记忆服务写入。"""
        project_id = state["project_id"]
        if state.get("source_id"):
            source = await memory.source(project_id, state["source_id"])
            result = await memory.write(source)
        else:
            result = await memory.consolidate(project_id)
        return {
            "source_id": None,
            "outcome": result["outcome"],
            "outcome_origin": result.get("outcome_origin", "model"),
        }

    return (
        StateGraph(MemoryInput)
        .add_node("remember", remember)
        .add_edge(START, "remember")
        .add_edge("remember", END)
        .compile(store=memory.store)
    )
