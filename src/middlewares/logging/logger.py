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
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command
from langgraph.config import get_config

TOKEN_USAGE_ATTRIBUTION_KEY = "token_usage_attribution"

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

    def _thread_tag(self, thread_id: str | None = None) -> str:
        return f" [{thread_id}]" if thread_id else ""

    def format_model_request(self, request: ModelRequest, duration_ms: float | None = None, thread_id: str | None = None, **kwargs) -> str:
        msg_count = len(request.messages) if request.messages else 0
        tools_count = len(request.tools) if request.tools else 0
        duration_str = f" | 耗时: {duration_ms:.0f}ms" if duration_ms else ""
        tag = self._thread_tag(thread_id)
        return f"[模型请求]{tag} 消息数: {msg_count} | 工具数: {tools_count}{duration_str}"

    def format_model_response(self, response: ModelResponse, duration_ms: float | None = None, token_usage: dict | None = None, thread_id: str | None = None, **kwargs) -> str:
        result = response.result[0] if response.result else None
        content_preview = ""
        tool_calls_count = 0

        if hasattr(result, 'content') and result.content:
            content_preview = result.content[:100] + "..." if len(result.content) > 100 else result.content
        if hasattr(result, 'tool_calls') and result.tool_calls:
            tool_calls_count = len(result.tool_calls)

        duration_str = f" | 耗时: {duration_ms:.0f}ms" if duration_ms else ""
        token_str = ""
        if token_usage:
            token_str = f" | token: ↑{token_usage.get('input_tokens', '?')} ↓{token_usage.get('output_tokens', '?')}"
        tag = self._thread_tag(thread_id)
        return f"[模型响应]{tag} 内容: {content_preview} | 工具调用: {tool_calls_count}{duration_str}{token_str}"

    def format_tool_call(self, request: ToolCallRequest, thread_id: str | None = None, **kwargs) -> str:
        tool_name = request.tool_call.get("name", "unknown")
        args = request.tool_call.get("args", {})
        args_str = json.dumps(args, ensure_ascii=False)[:200]
        tag = self._thread_tag(thread_id)
        return f"[工具调用]{tag} {tool_name}({args_str})"

    def format_tool_result(self, result: ToolMessage | Command, duration_ms: float | None = None, thread_id: str | None = None, **kwargs) -> str:
        duration_str = f" | 耗时: {duration_ms:.0f}ms" if duration_ms else ""
        tag = self._thread_tag(thread_id)
        if isinstance(result, ToolMessage):
            content = result.content[:200] if isinstance(result.content, str) else str(result.content)[:200]
            status = result.status if hasattr(result, 'status') else "success"
            return f"[工具结果]{tag} 状态: {status} | 内容: {content}{duration_str}"
        return f"[工具结果]{tag} Command{duration_str}"

    def format_token_usage(self, usage: dict, thread_id: str | None = None, **kwargs) -> str:
        tag = self._thread_tag(thread_id)
        return (
            f"[Token]{tag} ↑{usage.get('input_tokens', '?')} "
            f"↓{usage.get('output_tokens', '?')} "
            f"合计: {usage.get('total_tokens', '?')}"
        )


class JSONFormatter(LogFormatter):
    """JSON 格式化器"""

    def _base_dict(self, thread_id: str | None = None, **kwargs) -> dict:
        d: dict = {
            "timestamp": datetime.now().isoformat(),
            **kwargs,
        }
        if thread_id:
            d["thread_id"] = thread_id
        return d

    def format_model_request(self, request: ModelRequest, duration_ms: float | None = None, thread_id: str | None = None, **kwargs) -> str:
        data = self._base_dict(
            thread_id=thread_id,
            event="model_request",
            message_count=len(request.messages) if request.messages else 0,
            tools_count=len(request.tools) if request.tools else 0,
            duration_ms=duration_ms,
        )
        return json.dumps(data, ensure_ascii=False)

    def format_model_response(self, response: ModelResponse, duration_ms: float | None = None, token_usage: dict | None = None, thread_id: str | None = None, **kwargs) -> str:
        result = response.result[0] if response.result else None
        data = self._base_dict(
            thread_id=thread_id,
            event="model_response",
            has_content=bool(hasattr(result, 'content') and result.content),
            tool_calls_count=len(result.tool_calls) if hasattr(result, 'tool_calls') and result.tool_calls else 0,
            duration_ms=duration_ms,
            token_usage=token_usage,
        )
        return json.dumps(data, ensure_ascii=False)

    def format_tool_call(self, request: ToolCallRequest, thread_id: str | None = None, **kwargs) -> str:
        data = self._base_dict(
            thread_id=thread_id,
            event="tool_call",
            tool_name=request.tool_call.get("name", "unknown"),
            tool_args=request.tool_call.get("args", {}),
        )
        return json.dumps(data, ensure_ascii=False)

    def format_tool_result(self, result: ToolMessage | Command, duration_ms: float | None = None, thread_id: str | None = None, **kwargs) -> str:
        if isinstance(result, ToolMessage):
            data = self._base_dict(
                thread_id=thread_id,
                event="tool_result",
                status=result.status if hasattr(result, 'status') else "success",
                content_preview=str(result.content)[:200] if result.content else None,
                duration_ms=duration_ms,
            )
        else:
            data = self._base_dict(
                thread_id=thread_id,
                event="tool_result",
                type="command",
                duration_ms=duration_ms,
            )
        return json.dumps(data, ensure_ascii=False)

    def format_token_usage(self, usage: dict, thread_id: str | None = None, **kwargs) -> str:
        data = self._base_dict(
            thread_id=thread_id,
            event="token_usage",
            event_type="model",
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
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
            log_file: 日志文件路径（可选，仅支持绝对路径避免阻塞）
            logger: 自定义 logger（可选）
            formatter: 自定义格式化器（可选）
            include_state: 是否记录完整 state
            sensitive_keys: 敏感字段列表，自动脱敏
        """
        super().__init__()

        self.level = level
        self.include_state = include_state
        self.sensitive_keys = set(sensitive_keys or ["password", "token", "api_key", "secret"])
        self._log_file = log_file
        self._format = format

        # 设置 logger（惰性初始化，避免 blockbuster 检测到阻塞调用）
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

        # 设置格式化器
        if formatter:
            self.formatter = formatter
        elif format == "json":
            self.formatter = JSONFormatter()
        else:
            self.formatter = TextFormatter()

    def _ensure_file_handler(self) -> None:
        """惰性创建 FileHandler（避免构造时阻塞）"""
        if not self._log_file:
            return
        # 检查是否已添加
        for h in self.logger.handlers:
            if isinstance(h, logging.FileHandler):
                return
        try:
            file_handler = logging.FileHandler(self._log_file, encoding="utf-8")
            file_handler.setLevel(self.level)
            file_handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(file_handler)
        except Exception:
            pass

    def __getstate__(self):
        """排除不可序列化的属性（logger 内部有 _thread.lock）"""
        state = self.__dict__.copy()
        # 移除 logger，它包含不可序列化的 _thread.lock
        state['_logger_name'] = self.logger.name if hasattr(self, 'logger') else None
        state.pop('logger', None)
        return state

    def __setstate__(self, state):
        """恢复 logger"""
        logger_name = state.pop('_logger_name', None)
        self.__dict__.update(state)
        # 重新创建 logger
        if logger_name:
            self.logger = logging.getLogger(logger_name)
        else:
            self.logger = logging.getLogger(f"agent.logging.{id(self)}")
        # 确保 handler（StreamHandler）已添加
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setLevel(self.level)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)

    def _log(self, message: str, level: int | None = None):
        """输出日志"""
        self._ensure_file_handler()
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

    # ── Thread ID 提取 ──

    @staticmethod
    def _get_thread_id() -> str:
        try:
            config = get_config()
            return config.get("configurable", {}).get("thread_id", "unknown") or "unknown"
        except Exception:
            return "unknown"

    # === Agent 生命周期 ===

    def before_agent(self, state: AgentState, runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """Agent 开始执行"""
        thread_id = self._get_thread_id()
        self._log(f"[Session 开始] thread_id: {thread_id}")
        return None

    async def abefore_agent(self, state: AgentState, runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """Agent 开始执行（异步）"""
        return self.before_agent(state, runtime)

    def after_model(self, state: AgentState, runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """Agent 执行结束后的状态 + token 用量记录"""
        thread_id = self._get_thread_id()
        msg_count = len(state.get("messages", []))
        self._log(f"[Session 状态] thread_id: {thread_id} | 消息数: {msg_count}")

        # 记录 token 用量
        messages = state.get("messages", [])
        if messages:
            last = messages[-1]
            if isinstance(last, AIMessage):
                usage = getattr(last, "usage_metadata", None)
                if usage:
                    self._log(self.formatter.format_token_usage(
                        usage,
                        thread_id=thread_id,
                    ) if hasattr(self.formatter, 'format_token_usage') else (
                        f"[Token] ↑{usage.get('input_tokens', '?')} ↓{usage.get('output_tokens', '?')}"
                        f" 合计: {usage.get('total_tokens', '?')}"
                    ))
                    # 附加到 additional_kwargs 供前端消费
                    additional_kwargs = dict(getattr(last, "additional_kwargs", {}) or {})
                    if additional_kwargs.get(TOKEN_USAGE_ATTRIBUTION_KEY) != usage:
                        additional_kwargs[TOKEN_USAGE_ATTRIBUTION_KEY] = {
                            "version": 1,
                            "input_tokens": usage.get("input_tokens", 0),
                            "output_tokens": usage.get("output_tokens", 0),
                            "total_tokens": usage.get("total_tokens", 0),
                        }
                        # 用 model_copy 创建更新后的消息（保留所有原始字段）
                        updated_msg = last.model_copy(
                            update={"additional_kwargs": additional_kwargs}
                        )
                        return {"messages": [updated_msg]}
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
        thread_id = self._get_thread_id()

        # 记录请求
        self._log(self.formatter.format_model_request(request, thread_id=thread_id))

        try:
            # 执行模型调用
            response = handler(request)

            # 记录响应（含 token 用量）
            duration_ms = (time.perf_counter() - start_time) * 1000
            token_usage = None
            if response.result:
                result = response.result[0]
                if hasattr(result, 'usage_metadata') and getattr(result, 'usage_metadata'):
                    token_usage = getattr(result, 'usage_metadata')
            self._log(self.formatter.format_model_response(
                response, duration_ms=duration_ms, token_usage=token_usage, thread_id=thread_id,
            ))

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
        thread_id = self._get_thread_id()

        self._log(self.formatter.format_model_request(request, thread_id=thread_id))

        try:
            response = await handler(request)

            duration_ms = (time.perf_counter() - start_time) * 1000
            token_usage = None
            if response.result:
                result = response.result[0]
                if hasattr(result, 'usage_metadata') and getattr(result, 'usage_metadata'):
                    token_usage = getattr(result, 'usage_metadata')
            self._log(self.formatter.format_model_response(
                response, duration_ms=duration_ms, token_usage=token_usage, thread_id=thread_id,
            ))

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
        thread_id = self._get_thread_id()

        # 记录调用
        self._log(self.formatter.format_tool_call(request, thread_id=thread_id))

        try:
            # 执行工具
            result = handler(request)

            # 记录结果
            duration_ms = (time.perf_counter() - start_time) * 1000
            self._log(self.formatter.format_tool_result(result, duration_ms=duration_ms, thread_id=thread_id))

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
        thread_id = self._get_thread_id()

        self._log(self.formatter.format_tool_call(request, thread_id=thread_id))

        try:
            result = await handler(request)

            duration_ms = (time.perf_counter() - start_time) * 1000
            self._log(self.formatter.format_tool_result(result, duration_ms=duration_ms, thread_id=thread_id))

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