"""
自定义系统提示词中间件 — 支持动态替换/追加系统提示词

从 config.configurable 中读取以下参数：
- custom_system_prompt: str | None → 完全替换默认系统提示词
- append_system_prompt: str | None → 追加到默认系统提示词末尾
- user_profile: dict | None → 用户画像（grade_level, tech_stack 等）
- related_errors: list | None → 相关错题数据（联动错题本）

用法:
    from src.middlewares.agent.custom_prompt import create_custom_prompt_middleware

    middleware = create_custom_prompt_middleware()
    agent = create_agent(
        model="...",
        middleware=[middleware, ...],
    )

    # 调用时传入
    result = agent.invoke(
        {"messages": [("human", "教我变量")]},
        config={
            "configurable": {
                "user_profile": {"grade_level": "高一"},
                "append_system_prompt": "用简单语言讲解",
                "related_errors": [{"topic": "类型转换", "count": 3}],
            }
        },
    )
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langgraph.config import get_config

logger = logging.getLogger(__name__)


class CustomPromptMiddleware(AgentMiddleware):
    """自定义系统提示词中间件

    支持三种模式：
    1. 完全替换: configurable.custom_system_prompt
    2. 追加内容: configurable.append_system_prompt
    3. 智能注入: 根据 user_profile 和 related_errors 自动生成追加内容
    """

    def __init__(self, enabled: bool = True):
        super().__init__()
        self._enabled = enabled

    @staticmethod
    def _get_configurable() -> dict[str, Any]:
        """从 LangGraph runtime config 读取 configurable"""
        try:
            config = get_config()
            return config.get("configurable", {}) or {}
        except Exception:
            return {}

    def _build_profile_prompt(self, user_profile: dict) -> str:
        """根据用户画像生成提示词片段"""
        parts = []

        grade = user_profile.get("grade_level")
        if grade:
            if grade in ("初一", "初二", "初三", "高一", "高二", "高三"):
                parts.append(f"学生是{grade}学生，请用适合中学生的语言和例子讲解，避免过于超纲的内容。")
            elif grade in ("大一", "大二", "大三", "大四"):
                parts.append(f"学生是{grade}大学生，有一定学习基础，可以用更专业的术语和示例。")
            elif grade == "self_learner":
                parts.append("学生是自学者，请根据上下文判断其水平，讲解要通俗易懂但不幼稚。")

        tech_stack = user_profile.get("tech_stack")
        if tech_stack and isinstance(tech_stack, list) and tech_stack:
            parts.append(f"学生已掌握的技术栈：{', '.join(tech_stack)}。可以在此基础上建立类比和关联。")

        goal = user_profile.get("learning_goal")
        if goal:
            parts.append(f"学生的学习目标是：{goal}。讲解时请围绕这个目标组织内容。")

        hours = user_profile.get("hours_per_week")
        if hours:
            parts.append(f"学生每周可投入约 {hours} 小时学习，请据此控制每次讲解的深度和广度。")

        if not parts:
            return ""
        return "\n\n[学生画像]\n" + "\n".join(parts)

    def _build_errors_prompt(self, related_errors: list) -> str:
        """根据错题数据生成提示词片段"""
        if not related_errors:
            return ""

        parts = []
        for err in related_errors[:5]:  # 最多 5 条
            topic = err.get("knowledge_point") or err.get("topic", "未知")
            count = err.get("review_count") or err.get("count", 0)
            reason = err.get("error_reason", "")
            desc = f"- {topic}：错过 {count} 次"
            if reason:
                desc += f"（原因：{reason}）"
            parts.append(desc)

        if not parts:
            return ""
        return "\n\n[学生错题记录 - 请针对性强调]\n" + "\n".join(parts)

    def _build_appended_prompt(self, configurable: dict) -> str:
        """组装需要追加的所有提示词"""
        parts = []

        # 用户手动追加的内容
        append = configurable.get("append_system_prompt")
        if append:
            parts.append(append)

        # 用户画像自动生成
        user_profile = configurable.get("user_profile")
        if user_profile and isinstance(user_profile, dict):
            profile_prompt = self._build_profile_prompt(user_profile)
            if profile_prompt:
                parts.append(profile_prompt)

        # 错题联动
        related_errors = configurable.get("related_errors")
        if related_errors and isinstance(related_errors, list):
            errors_prompt = self._build_errors_prompt(related_errors)
            if errors_prompt:
                parts.append(errors_prompt)

        return "\n".join(parts)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if not self._enabled:
            return await handler(request)

        configurable = self._get_configurable()
        if not configurable:
            return await handler(request)

        overrides: dict[str, Any] = {}

        # 模式 1: 完全替换
        custom_prompt = configurable.get("custom_system_prompt")
        if custom_prompt:
            overrides["system_message"] = custom_prompt
        else:
            # 模式 2+3: 追加
            appended = self._build_appended_prompt(configurable)
            if appended:
                # 获取当前 system message 并追加
                current_system = getattr(request, "system_message", None)
                if current_system:
                    new_content = str(current_system) + "\n" + appended
                    overrides["system_message"] = new_content
                else:
                    overrides["system_message"] = appended

        if overrides:
            request = request.override(**overrides)

        return await handler(request)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        import asyncio
        return asyncio.run(self.awrap_model_call(request, handler))


# ═══════════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════════


def create_custom_prompt_middleware(enabled: bool = True) -> CustomPromptMiddleware:
    """创建自定义提示词中间件

    Args:
        enabled: 是否启用

    Returns:
        CustomPromptMiddleware 实例
    """
    return CustomPromptMiddleware(enabled=enabled)
