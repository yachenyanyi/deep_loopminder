"""
详细日志记录中间件

记录 agent 执行过程中的所有关键事件：
- 模型调用（请求/响应/耗时）
- 工具调用（参数/结果/耗时）
- Agent 生命周期事件
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
    ToolCallRequest,
)
from langchain_core.messages import ToolMessage
from langgraph.types import Command

if TYPE_CHECKING:
    from langgraph.runtime import Runtime


class LogFormatter:
    """日志格式化器基类"""

    def format_model_request(self, request: ModelRequest, **kwargs) -> str:
        raise NotImplementedError

    def format_model_response(self, response: ModelResponse, **kwargs) -> str:
        raise NotImplementedError

    def format_tool_call(self, request: ToolCallRequest, **kwargs) -> str:
        raise NotImplementedError

    def format_tool_result(self, result: ToolMessage | Command, **kwargs) -> str:
        raise NotImplementedError


class TextFormatter(LogFormatter):
    """文本格式化器"""

    def format_model_request(self, request: ModelRequest, duration_ms: float | None = None, **kwargs) -> str:
        msg_count = len(request.messages) if request.messages else 0
        tools_count = len(request.tools) if request.tools else 0
        duration_str = f" | 耗时: {duration_ms:.0f}ms" if duration_ms else ""
        return f"[模型请求] 消息数: {msg_count} | 工具数: {tools_count}{duration_str}"

    def format_model_response(self, response: ModelResponse, duration_ms: float | None = None, **kwargs) -> str:
        result = response.result[0] if response.result else None
        content_preview = ""
        tool_calls_count = 0

        if hasattr(result, 'content') and result.content:
            content_preview = result.content[:100] + "..." if len(result.content) > 100 else result.content
        if hasattr(result, 'tool_calls') and result.tool_calls:
            tool_calls_count = len(result.tool_calls)

        duration_str = f" | 耗时: {duration_ms:.0f}ms" if duration_ms else ""
        return f"[模型响应] 内容: {content_preview} | 工具调用: {tool_calls_count}{duration_str}"

    def format_tool_call(self, request: ToolCallRequest, **kwargs) -> str:
        tool_name = request.tool_call.get("name", "unknown")
        args = request.tool_call.get("args", {})
        args_str = json.dumps(args, ensure_ascii=False)[:200]
        return f"[工具调用] {tool_name}({args_str})"

    def format_tool_result(self, result: ToolMessage | Command, duration_ms: float | None = None, **kwargs) -> str:
        duration_str = f" | 耗时: {duration_ms:.0f}ms" if duration_ms else ""
        if isinstance(result, ToolMessage):
            content = result.content[:200] if isinstance(result.content, str) else str(result.content)[:200]
            status = result.status if hasattr(result, 'status') else "success"
            return f"[工具结果] 状态: {status} | 内容: {content}{duration_str}"
        return f"[工具结果] Command{duration_str}"


class JSONFormatter(LogFormatter):
    """JSON 格式化器"""

    def _base_dict(self, **kwargs) -> dict:
        return {
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }

    def format_model_request(self, request: ModelRequest, duration_ms: float | None = None, **kwargs) -> str:
        data = self._base_dict(
            event="model_request",
            message_count=len(request.messages) if request.messages else 0,
            tools_count=len(request.tools) if request.tools else 0,
            duration_ms=duration_ms,
        )
        return json.dumps(data, ensure_ascii=False)

    def format_model_response(self, response: ModelResponse, duration_ms: float | None = None, **kwargs) -> str:
        result = response.result[0] if response.result else None
        data = self._base_dict(
            event="model_response",
            has_content=bool(hasattr(result, 'content') and result.content),
            tool_calls_count=len(result.tool_calls) if hasattr(result, 'tool_calls') and result.tool_calls else 0,
            duration_ms=duration_ms,
        )
        return json.dumps(data, ensure_ascii=False)

    def format_tool_call(self, request: ToolCallRequest, **kwargs) -> str:
        data = self._base_dict(
            event="tool_call",
            tool_name=request.tool_call.get("name", "unknown"),
            tool_args=request.tool_call.get("args", {}),
        )
        return json.dumps(data, ensure_ascii=False)

    def format_tool_result(self, result: ToolMessage | Command, duration_ms: float | None = None, **kwargs) -> str:
        if isinstance(result, ToolMessage):
            data = self._base_dict(
                event="tool_result",
                status=result.status if hasattr(result, 'status') else "success",
                content_preview=str(result.content)[:200] if result.content else None,
                duration_ms=duration_ms,
            )
        else:
            data = self._base_dict(
                event="tool_result",
                type="command",
                duration_ms=duration_ms,
            )
        return json.dumps(data, ensure_ascii=False)


class LoggingMiddleware(AgentMiddleware):
    """详细日志记录中间件

    使用方法：
    ```python
    from src.middlewares.logging import LoggingMiddleware

    # 基础使用（输出到控制台）
    middleware = LoggingMiddleware()

    # 输出到文件
    middleware = LoggingMiddleware(log_file="agent.log")

    # JSON 格式
    middleware = LoggingMiddleware(format="json", log_file="agent.jsonl")

    # 自定义日志级别
    middleware = LoggingMiddleware(level=logging.DEBUG)
    ```
    """

    def __init__(
        self,
        level: int = logging.INFO,
        format: str = "text",  # "text" or "json"
        log_file: str | None = None,
        logger: logging.Logger | None = None,
        formatter: LogFormatter | None = None,
        include_state: bool = False,
        sensitive_keys: list[str] | None = None,
    ):
        """
        Args:
            level: 日志级别
            format: 日志格式 ("text" 或 "json")
            log_file: 日志文件路径（可选）
            logger: 自定义 logger（可选）
            formatter: 自定义格式化器（可选）
            include_state: 是否记录完整 state
            sensitive_keys: 敏感字段列表，自动脱敏
        """
        super().__init__()

        self.level = level
        self.include_state = include_state
        self.sensitive_keys = set(sensitive_keys or ["password", "token", "api_key", "secret"])

        # 设置 logger
        if logger:
            self.logger = logger
        else:
            self.logger = logging.getLogger(f"agent.logging.{id(self)}")
            self.logger.setLevel(level)

            if not self.logger.handlers:
                handler = logging.StreamHandler()
                handler.setLevel(level)
                handler.setFormatter(logging.Formatter("%(message)s"))
                self.logger.addHandler(handler)

                if log_file:
                    file_handler = logging.FileHandler(log_file, encoding="utf-8")
                    file_handler.setLevel(level)
                    file_handler.setFormatter(logging.Formatter("%(message)s"))
                    self.logger.addHandler(file_handler)

        # 设置格式化器
        if formatter:
            self.formatter = formatter
        elif format == "json":
            self.formatter = JSONFormatter()
        else:
            self.formatter = TextFormatter()

    def _log(self, message: str, level: int | None = None):
        """输出日志"""
        self.logger.log(level or self.level, message)

    def _mask_sensitive(self, data: dict) -> dict:
        """脱敏敏感字段"""
        result = {}
        for key, value in data.items():
            if key.lower() in self.sensitive_keys:
                result[key] = "***MASKED***"
            elif isinstance(value, dict):
                result[key] = self._mask_sensitive(value)
            else:
                result[key] = value
        return result

    # === Agent 生命周期 ===

    def before_agent(self, state: AgentState, runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """Agent 开始执行"""
        thread_id = runtime.config.get("configurable", {}).get("thread_id", "unknown")
        self._log(f"[Session 开始] thread_id: {thread_id}")
        return None

    async def abefore_agent(self, state: AgentState, runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """Agent 开始执行（异步）"""
        return self.before_agent(state, runtime)

    def after_model(self, state: AgentState, runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """Agent 执行结束后的状态"""
        msg_count = len(state.get("messages", []))
        self._log(f"[Session 状态] 当前消息数: {msg_count}")
        return None

    async def aafter_model(self, state: AgentState, runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """Agent 执行结束后的状态（异步）"""
        return self.after_model(state, runtime)

    # === 模型调用 ===

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """包装模型调用，记录请求和响应"""
        start_time = time.perf_counter()

        # 记录请求
        self._log(self.formatter.format_model_request(request))

        try:
            # 执行模型调用
            response = handler(request)

            # 记录响应
            duration_ms = (time.perf_counter() - start_time) * 1000
            self._log(self.formatter.format_model_response(response, duration_ms=duration_ms))

            return response

        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            self._log(f"[模型错误] {type(e).__name__}: {e} | 耗时: {duration_ms:.0f}ms", level=logging.ERROR)
            raise

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """包装模型调用（异步）"""
        start_time = time.perf_counter()

        self._log(self.formatter.format_model_request(request))

        try:
            response = await handler(request)

            duration_ms = (time.perf_counter() - start_time) * 1000
            self._log(self.formatter.format_model_response(response, duration_ms=duration_ms))

            return response

        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            self._log(f"[模型错误] {type(e).__name__}: {e} | 耗时: {duration_ms:.0f}ms", level=logging.ERROR)
            raise

    # === 工具调用 ===

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """包装工具调用，记录参数和结果"""
        start_time = time.perf_counter()

        # 记录调用
        self._log(self.formatter.format_tool_call(request))

        try:
            # 执行工具
            result = handler(request)

            # 记录结果
            duration_ms = (time.perf_counter() - start_time) * 1000
            self._log(self.formatter.format_tool_result(result, duration_ms=duration_ms))

            return result

        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            tool_name = request.tool_call.get("name", "unknown")
            self._log(f"[工具错误] {tool_name}: {type(e).__name__}: {e} | 耗时: {duration_ms:.0f}ms", level=logging.ERROR)
            raise

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """包装工具调用（异步）"""
        start_time = time.perf_counter()

        self._log(self.formatter.format_tool_call(request))

        try:
            result = await handler(request)

            duration_ms = (time.perf_counter() - start_time) * 1000
            self._log(self.formatter.format_tool_result(result, duration_ms=duration_ms))

            return result

        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            tool_name = request.tool_call.get("name", "unknown")
            self._log(f"[工具错误] {tool_name}: {type(e).__name__}: {e} | 耗时: {duration_ms:.0f}ms", level=logging.ERROR)
            raise


def create_logging_middleware(
    level: int = logging.INFO,
    format: str = "text",
    log_file: str | None = None,
    **kwargs,
) -> LoggingMiddleware:
    """创建日志中间件的便捷函数"""
    return LoggingMiddleware(
        level=level,
        format=format,
        log_file=log_file,
        **kwargs,
    )