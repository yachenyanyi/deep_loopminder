"""后台记忆图入口：完整的任务执行功能集中在此文件。"""

from langgraph.graph import END, START, StateGraph

from .jobs import MemoryInput, PMMemory
from .provider import get_pm_memory_store


async def pm_memory_agent():
    """创建后台记忆图，统一处理来源快照和官方定期整理任务。"""
    store = (await get_pm_memory_store()).store

    async def remember(state: MemoryInput):
        """读取固定来源或采集整理快照，再交给记忆服务写入。"""
        project_id = state["project_id"]
        source = await PMMemory(store).source(project_id, state["source_id"]) if state.get("source_id") else None
        profile = source.get("model_profile") if source else None
        from src.middlewares.agent.user_configurable import configured_pm_model, pm_model_catalog

        catalog = pm_model_catalog(store)
        if source is None:
            profile = await PMMemory(store).consolidation_model_profile(project_id)
        elif profile is None:
            raise ValueError("历史记忆来源缺少可核验的模型选择，不能自动发送")
        if profile is not None:
            if not isinstance(profile, dict) or profile.get("mode") != "catalog":
                raise ValueError("记忆来源未使用当前模型目录，不能自动发送")
            if not isinstance(profile.get("endpoint_hash"), str):
                raise ValueError("记忆来源的模型服务地址无效")
            selected_model = await configured_pm_model(catalog, profile.get("selection"),
                                                       expected_endpoint_hash=profile["endpoint_hash"])
        else:
            selected = await catalog.get_default()
            if selected is None:
                raise ValueError("尚未配置默认模型，请在设置中选择已启用的模型")
            selected_model = await configured_pm_model(catalog, selected)
        memory = PMMemory(store, selected_model)
        if source is not None:
            # Ordinary reflection and handoffs can also read memory files.
            # Use the same attributed frozen view as consolidation so a model
            # switch cannot disclose older files to another provider.
            result = await memory.write(source, verify_consolidation_profile=True)
        else:
            result = await memory.consolidate(project_id, model_profile=profile, verify_model_profile=True)
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
        .compile(store=store)
    )


async def pm_memory_agent_server():
    """Expose a zero-argument graph factory to Agent Server."""
    return await pm_memory_agent()
