"""
自动对话标题中间件 — 首轮对话后自动生成标题

原理：
  在 after_model 钩子中检测到首次完整对话（1用户消息 + 1AI回复），
  异步调用轻量模型生成标题，通过返回值合并进 LangGraph state，
  最终被 checkpointer 持久化到数据库。

用法:
    from src.middlewares.agent.title import create_title_middleware

    agent = create_agent(
        middleware=[create_title_middleware(), ...],
    )

    前端通过 LangGraph SDK 读取:
        state = await client.threads.get_state(thread_id)
        state["values"]["title"]
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, override

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import BaseMessage
from langgraph.config import get_config
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

_TITLE_PROMPT = """为这段对话生成一个简短标题（{max_words}个字以内）。

用户消息: {user_msg}
助手回复: {assistant_msg}

要求：
- 概括对话核心主题
- {max_words}个字以内
- 只用中文
- 不要引号，不要标点
- 直接输出标题文本
"""


class TitleMiddleware(AgentMiddleware):
    """自动对话标题中间件

    首次完整对话后生成标题。同步路径不回退到 LLM（不阻塞 agent 循环），
    异步路径先尝试 LLM 生成，失败则 fallback 到截取用户消息。
    """

    def __init__(
        self,
        *,
        model=None,
        max_words: int = 10,
        max_chars: int = 50,
        enabled: bool = True,
    ):
        super().__init__()
        self._model = model
        self._max_words = max_words
        self._max_chars = max_chars
        self._enabled = enabled

    # ── 标题生成条件 ──

    def _should_generate(self, state: AgentState) -> bool:
        if not self._enabled:
            return False
        if state.get("title"):
            return False

        messages: list[BaseMessage] = state.get("messages", [])
        if len(messages) < 2:
            return False

        user_msgs = [m for m in messages if getattr(m, "type", None) == "human"]
        ai_msgs = [m for m in messages if getattr(m, "type", None) == "ai"]

        return len(user_msgs) == 1 and len(ai_msgs) >= 1

    # ── 内容处理 ──

    @staticmethod
    def _normalize(content: object) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [TitleMiddleware._normalize(item) for item in content]
            return "\n".join(p for p in parts if p)
        if isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str):
                return text
            nested = content.get("content")
            if nested is not None:
                return TitleMiddleware._normalize(nested)
        return ""

    @staticmethod
    def _strip_think(text: str) -> str:
        return re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()

    def _parse_title(self, content: object) -> str:
        raw = self._normalize(content)
        raw = self._strip_think(raw)
        title = raw.strip().strip('"').strip("'").strip()
        return title[: self._max_chars] if len(title) > self._max_chars else title

    def _fallback_title(self, state: AgentState) -> str:
        messages: list[BaseMessage] = state.get("messages", [])
        user_msg = next(
            (self._normalize(m.content) for m in messages if getattr(m, "type", None) == "human"),
            "",
        )
        if len(user_msg) > 50:
            return user_msg[:50].rstrip() + "..."
        return user_msg if user_msg else "新对话"

    # ── 同步路径（仅 fallback，不调 LLM，不阻塞） ──

    def _sync_result(self, state: AgentState) -> dict | None:
        if not self._should_generate(state):
            return None
        return {"title": self._fallback_title(state)}

    # ── 异步路径（尝试 LLM 生成，失败 fallback） ──

    async def _async_result(self, state: AgentState) -> dict | None:
        if not self._should_generate(state):
            return None

        messages: list[BaseMessage] = state.get("messages", [])
        user_msg = next(
            (self._normalize(m.content) for m in messages if getattr(m, "type", None) == "human"),
            "",
        )
        ai_msg = next(
            (self._strip_think(self._normalize(m.content)) for m in messages if getattr(m, "type", None) == "ai"),
            "",
        )

        # 尝试 LLM 生成
        if self._model is not None and user_msg and ai_msg:
            prompt = _TITLE_PROMPT.format(
                max_words=self._max_words,
                user_msg=user_msg[:500],
                assistant_msg=ai_msg[:500],
            )
            try:
                response = await self._model.ainvoke(prompt)
                title = self._parse_title(response.content)
                if title:
                    return {"title": title}
            except Exception:
                logger.debug("LLM 标题生成失败，使用 fallback", exc_info=True)

        return {"title": self._fallback_title(state)}

    # ── AgentMiddleware 钩子 ──

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._sync_result(state)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return await self._async_result(state)


def create_title_middleware(
    model=None,
    max_words: int = 10,
    max_chars: int = 50,
    enabled: bool = True,
) -> TitleMiddleware:
    """创建自动标题中间件

    Args:
        model: 用于标题生成的轻量模型。为 None 时只用 fallback（截取用户消息）。
        max_words: 标题最大字数（提示词中指定）
        max_chars: 标题最大字符数（截断保护）
        enabled: 是否启用

    Returns:
        TitleMiddleware 实例
    """
    return TitleMiddleware(
        model=model,
        max_words=max_words,
        max_chars=max_chars,
        enabled=enabled,
    )
