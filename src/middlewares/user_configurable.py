"""UserConfigurableMiddleware - 用户可选模型/提示词中间件

每次 LLM 调用前从 RunnableConfig.configurable 中读取 model 和 system_prompt，
动态替换本次调用的模型和系统提示词。

使用方式：
    from src.middlewares.user_configurable import UserConfigurableMiddleware

    agent = create_deep_agent(
        model="deepseek:deepseek-chat",
        middleware=[UserConfigurableMiddleware()],
    )

    result = agent.invoke(
        {"messages": [{"role": "user", "content": "你好"}]},
        config={
            "configurable": {
                "model": "anthropic:claude-sonnet-4-6",
                "system_prompt": "用温柔的语气回复。",
            }
        },
    )

参数说明（均放在 config.configurable 中）：
    - model: str | None  →  传入则切换模型（provider:model 格式），不传则用 Agent 默认模型
    - system_prompt: str | None  →  传入则替换系统提示词，不传则用 Agent 默认提示词
    - 两个参数都是可选的，互不影响
"""

from __future__ import annotations

import logging
from typing import Callable, Awaitable, Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain.chat_models import init_chat_model


class UserConfigurableMiddleware(AgentMiddleware):
    """允许用户在每次调用时动态指定模型和系统提示词。

    从 config.configurable 中读取以下可选参数：
    - model: str | None  →  override 模型
    - system_prompt: str | None  →  override 系统提示词
    """

    @staticmethod
    def _get_configurable(request: ModelRequest) -> dict[str, Any] | None:
        """从 ModelRequest 中提取 configurable 字典。"""
        try:
            from langgraph.config import get_config
            config = get_config()
            return config.get("configurable", {}) if isinstance(config, dict) else None
        except RuntimeError:
            return None

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        configurable = self._get_configurable(request)
        if not configurable:
            return await handler(request)

        overrides: dict[str, Any] = {}

        # 用户指定了模型 → 切换
        if configurable.get("model"):
            try:
                overrides["model"] = init_chat_model(configurable["model"])
            except Exception:
                logging.warning(
                    "UserConfigurableMiddleware: 无法加载模型 '%s'，使用默认模型",
                    configurable["model"],
                )

        # 用户指定了提示词 → 替换
        if configurable.get("system_prompt"):
            overrides["system_message"] = configurable["system_prompt"]

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
