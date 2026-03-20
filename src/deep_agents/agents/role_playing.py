from deepagents import create_deep_agent
from deepagents.backends import StateBackend, CompositeBackend
from src.models.llm import default_model
from src.backend.backend import NamespacedStoreBackend
from src.deep_agents.db import init_postgres_checkpointer, init_postgres_store
from src.deep_agents.create_custom_agents.deep_custom_agent import create_custom_agent
from src.deep_agents.config import load_prompt_from_file
from src.middlewares import role_playing_summary

# 异步创建角色扮演代理，修改了deepagent，自己玩着用，适合网页端
async def create_role_playing_agent():
    """异步创建角色扮演代理，使用PostgreSQL持久化存储"""
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()

    return create_custom_agent(
        model=default_model,
        tools=[],
        system_prompt=load_prompt_from_file('src/deep_agents/test.txt')+"在每个章节结束后，将章节内容总结一下，保存到/chapter/{第n章-章节名}.md,当你在前文中对章节信息不了解时，请使用工具读取/chapter/{第n章-章节名}.md",

        # 存储策略：混合使用短期和长期记忆
        backend=lambda rt: CompositeBackend(
            default=StateBackend(rt),
            routes={

                "/chapter/": NamespacedStoreBackend(rt, ("{user_id}", "{thread_id}"), store=postgres_store)
            }
        ),

        # 使用PostgreSQL存储作为BaseStore实例
        store=postgres_store,

        # 配置checkpointer用于线程级别的对话记忆 - 使用 PostgreSQL 持久化存储
        checkpointer=postgres_checkpointer,

        # 子代理配置
        subagents=[

        ],

        # 角色扮演特定的中间件配置
        middleware=[role_playing_summary]


    )