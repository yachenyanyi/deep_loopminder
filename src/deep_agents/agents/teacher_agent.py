"""
教授代理 (Teacher Agent) — 利用B站资源进行交互式教学

使用 BilibiliMiddleware 搜索B站视频并提取AI字幕，将提取的内容
与自身知识相结合，提供系统性、结构化的教学讲解。

用法:
    from src.deep_agents.agents import create_teacher_agent

    agent = await create_teacher_agent()
    result = await agent.ainvoke({"messages": [("human", "教我一个主题")]})
"""

import os
from typing import NotRequired

from langchain.agents import create_agent
from langchain.agents.middleware import AgentState

from src.deep_agents.config import BASE_DIR, SKILLS_REPO_DIR, load_prompt_from_file
from src.deep_agents.db import init_postgres_checkpointer, init_postgres_store
from src.middlewares.tools.bilibili import create_bilibili_middleware
from src.middlewares.tools.error_book import create_error_book_middleware
from src.middlewares.tools.roadmap import create_roadmap_middleware
from src.middlewares.tools.quiz import create_quiz_middleware
from src.middlewares.logging.logger import create_logging_middleware
from src.middlewares.error.llm_handler import create_llm_error_handling_middleware
from src.middlewares.error.tool_handler import create_tool_error_handling_middleware
from src.middlewares.agent.title import create_title_middleware
from src.middlewares.agent.custom_prompt import create_custom_prompt_middleware
from src.middlewares.agent.skills_tool import create_skills_tool_middleware
from src.models.llm import get_default_model


class TeacherAgentState(AgentState):
    """Teacher Agent 的 state schema，添加 title 字段"""
    title: NotRequired[str | None]


async def create_teacher_agent():
    """异步创建教授代理，仅包含 B站搜索和字幕提取工具。

    代理的特点:
    - 仅暴露 bili_search 和 bili_extract 两个工具
    - 提取视频 AI 字幕作为教学素材
    - 使用 PostgreSQL 持久化存储对话历史
    - 4层中间件栈：LLM 错误处理 → 审计日志 → 工具错误处理 → B站工具

    Returns:
        CompiledStateGraph: 配置好的教授代理实例
    """
    postgres_checkpointer = await init_postgres_checkpointer()
    postgres_store = await init_postgres_store()
    log_file = os.path.join(BASE_DIR, "logs", "teacher_agent.log")
    logging_middleware = create_logging_middleware(log_file=log_file)
    bilibili_middleware = create_bilibili_middleware()
    error_handling_middleware = create_llm_error_handling_middleware()
    tool_error_middleware = create_tool_error_handling_middleware()
    error_book_middleware = create_error_book_middleware()
    roadmap_middleware = create_roadmap_middleware()
    quiz_middleware = create_quiz_middleware()
    custom_prompt_middleware = create_custom_prompt_middleware()

    # 标题生成和主模型用同一个
    title_middleware = create_title_middleware(
        model=get_default_model(),
        max_words=10,
        max_chars=50,
    )

    # Skills: 渐进式加载（Level 1 注入摘要 + Level 2 load_skill 工具读取完整内容）
    skills_dir = os.path.join(SKILLS_REPO_DIR, "skills")
    skills_middleware = create_skills_tool_middleware(skills_dir=skills_dir)

    # 加载系统提示词（Skill 由 SkillsToolMiddleware 按需注入）
    system_prompt = load_prompt_from_file("src/deep_agents/teacher_prompt.txt")

    return create_agent(
        model=get_default_model(),
        tools=[],
        state_schema=TeacherAgentState,
        system_prompt=system_prompt,#"你是用户的ai助手"
        store=postgres_store,
        checkpointer=postgres_checkpointer,
        middleware=[
            error_handling_middleware,   # 最外层: LLM 重试 + 熔断 + 降级
            logging_middleware,          # 审计日志 + token 追踪
            tool_error_middleware,       # 工具异常 → ToolMessage
            custom_prompt_middleware,    # 自定义提示词 + 用户画像 + 错题联动
            skills_middleware,           # Skill 渐进式加载（roadmap-planner 等）
            title_middleware,            # 自动对话标题
            error_book_middleware,       # 错题本读写
            roadmap_middleware,          # 学习路线图工具
            quiz_middleware,             # AI 出题 + 自动存题库
            bilibili_middleware,         # 最内层: B站搜索 + 字幕
        ],
    )
