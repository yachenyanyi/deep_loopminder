"""
工具错误处理中间件 — 将工具异常转为 ToolMessage(status="error")

当工具执行抛出异常时，不崩溃 agent 循环，而是返回结构化的错误 ToolMessage，
agent 可以继续执行或选择其他工具。

用法:
    from src.middlewares.error.tool import ToolErrorHandlingMiddleware

    agent = create_agent(
        middleware=[ToolErrorHandlingMiddleware(), ...],
    )
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)

_MISSING_TOOL_CALL_ID = "missing_tool_call_id"


class ToolErrorHandlingMiddleware(AgentMiddleware):
    """工具错误处理中间件

    捕获工具执行中的异常，转换为 ``ToolMessage(status="error")``，
    让 agent 循环可以继续而不是崩溃。

    ``GraphBubbleUp``（LangGraph 控制信号）会被透传，
    不会被此中间件捕获。
    """

    def _build_error_message(self, request: ToolCallRequest, exc: Exception) -> ToolMessage:
        """构造错误 ToolMessage"""
        tool_name = str(request.tool_call.get("name") or "unknown_tool")
        tool_call_id = str(request.tool_call.get("id") or _MISSING_TOOL_CALL_ID)
        detail = str(exc).strip() or exc.__class__.__name__
        if len(detail) > 500:
            detail = detail[:497] + "..."

        content = (
            f"工具 '{tool_name}' 执行失败: {exc.__class__.__name__}: {detail}。"
            f"请用已有信息继续，或换一个工具。"
        )
        return ToolMessage(
            content=content,
            tool_call_id=tool_call_id,
            name=tool_name,
            status="error",
        )

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        try:
            return handler(request)
        except GraphBubbleUp:
            # LangGraph 控制信号（interrupt/pause/resume）必须透传
            raise
        except Exception as exc:
            logger.exception(
                "工具执行失败 (sync): name=%s id=%s",
                request.tool_call.get("name"),
                request.tool_call.get("id"),
            )
            return self._build_error_message(request, exc)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        try:
            return await handler(request)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            logger.exception(
                "工具执行失败 (async): name=%s id=%s",
                request.tool_call.get("name"),
                request.tool_call.get("id"),
            )
            return self._build_error_message(request, exc)


def create_tool_error_handling_middleware() -> ToolErrorHandlingMiddleware:
    """创建工具错误处理中间件"""
    return ToolErrorHandlingMiddleware()
