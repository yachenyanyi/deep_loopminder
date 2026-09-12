"""
LLM 错误处理中间件 — 重试 + 熔断器 + 降级消息

功能：
1. **错误分类** — 区分 quota / auth / rate_limit / transient / 不可恢复错误
2. **自动重试** — 指数退避，仅重试可恢复错误（超时、5xx、限流）
3. **熔断器** — 连续失败达到阈值后断开，恢复超时后 half-open 探测
4. **降级消息** — 返回友好的中文 AIMessage，不崩溃

用法:
    from src.middlewares.error.llm import LLMErrorHandlingMiddleware

    agent = create_agent(
        model=...,
        middleware=[LLMErrorHandlingMiddleware(), ...],
    )
"""

from __future__ import annotations

import asyncio
import logging
import time
import threading
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage
from langgraph.errors import GraphBubbleUp

logger = logging.getLogger(__name__)

# ── 可重试的 HTTP 状态码 ──
_RETRIABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}

# ── 错误消息关键词匹配 ──
_BUSY_PATTERNS = (
    "server busy", "temporarily unavailable", "try again later",
    "please retry", "please try again", "overloaded", "high demand",
    "rate limit", "负载较高", "服务繁忙", "稍后重试", "请稍后重试",
)
_QUOTA_PATTERNS = (
    "insufficient_quota", "quota", "billing", "credit", "payment",
    "余额不足", "超出限额", "额度不足", "欠费",
)
_AUTH_PATTERNS = (
    "authentication", "unauthorized", "invalid api key",
    "invalid_api_key", "permission", "forbidden", "access denied",
    "无权", "未授权",
)


class LLMErrorHandlingMiddleware(AgentMiddleware):
    """LLM 调用错误处理中间件

    包装模型调用，自动处理临时错误（超时、5xx、限流），
    对永久性错误（认证、配额不足）直接返回友好提示。
    熔断器防止连续失败时持续重试。

    Args:
        max_retries: 最大重试次数（默认 3）
        base_delay_ms: 退避起始毫秒数（默认 1000）
        cap_delay_ms: 退避上限毫秒数（默认 8000）
        circuit_breaker_threshold: 触发熔断的连续失败次数（默认 3）
        circuit_breaker_timeout: 熔断恢复超时秒数（默认 60）
    """

    def __init__(
        self,
        *,
        max_retries: int = 3,
        base_delay_ms: int = 1000,
        cap_delay_ms: int = 8000,
        circuit_breaker_threshold: int = 3,
        circuit_breaker_timeout: int = 60,
    ) -> None:
        super().__init__()
        self.max_retries = max_retries
        self.base_delay_ms = base_delay_ms
        self.cap_delay_ms = cap_delay_ms
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.circuit_breaker_timeout = circuit_breaker_timeout

        # 熔断器状态（线程安全）
        self._lock = threading.Lock()
        self._failure_count = 0
        self._state = "closed"  # closed | open | half_open
        self._open_until = 0.0
        self._probe_in_flight = False

    # ── 序列化兼容 ──

    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        state.pop("_lock", None)
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._lock = threading.Lock()

    # ════════════════════════════════════════════════════════════════
    # 熔断器
    # ════════════════════════════════════════════════════════════════

    def _circuit_open(self) -> bool:
        """返回 True 表示熔断器断开（快速失败）"""
        with self._lock:
            now = time.time()

            if self._state == "open":
                if now < self._open_until:
                    return True
                self._state = "half_open"
                self._probe_in_flight = False

            if self._state == "half_open":
                if self._probe_in_flight:
                    return True
                self._probe_in_flight = True
                return False

            return False

    def _record_success(self) -> None:
        """记录成功，重置熔断器"""
        with self._lock:
            if self._state != "closed" or self._failure_count > 0:
                logger.info("🟢 熔断器已复位 (closed)")
            self._failure_count = 0
            self._state = "closed"
            self._open_until = 0.0
            self._probe_in_flight = False

    def _record_failure(self) -> None:
        """记录失败，可能触发熔断"""
        with self._lock:
            if self._state == "half_open":
                self._open_until = time.time() + self.circuit_breaker_timeout
                self._state = "open"
                self._probe_in_flight = False
                logger.error(
                    "🔴 熔断器探测失败 (open)，%ds 后重试",
                    self.circuit_breaker_timeout,
                )
                return

            self._failure_count += 1
            if self._failure_count >= self.circuit_breaker_threshold:
                self._open_until = time.time() + self.circuit_breaker_timeout
                if self._state != "open":
                    self._state = "open"
                    self._probe_in_flight = False
                    logger.error(
                        "🔴 熔断器触发 (open)，连续失败 %d 次，%ds 后探测",
                        self.circuit_breaker_threshold,
                        self.circuit_breaker_timeout,
                    )

    # ════════════════════════════════════════════════════════════════
    # 错误分类
    # ════════════════════════════════════════════════════════════════

    def _classify_error(self, exc: BaseException) -> tuple[bool, str]:
        """分类错误。返回 (是否可重试, 分类名)"""
        detail = str(exc).lower()
        exc_name = type(exc).__name__
        status_code = self._extract_status_code(exc)

        # 配额不足 — 不可恢复
        if any(p in detail for p in _QUOTA_PATTERNS):
            return False, "quota"

        # 认证错误 — 不可恢复
        if any(p in detail for p in _AUTH_PATTERNS):
            return False, "auth"

        # 限流 — 优先判断，与普通 5xx 区分
        if status_code == 429 or exc_name == "RateLimitError" or "rate limit" in detail:
            return True, "rate_limit"

        # 通用 HTTP 状态码 — 可重试
        if status_code in _RETRIABLE_STATUS_CODES:
            return True, "transient"

        # 特定异常类名 — 可重试
        if exc_name in {
            "APITimeoutError",
            "APIConnectionError",
            "TimeoutException",
            "ConnectTimeout",
            "ReadTimeout",
            "ConnectError",
            "RemoteProtocolError",
            "ReadError",
            "InternalServerError",
        }:
            return True, "transient"

        # 消息文本中的可重试关键词（捕获自定义异常包装）
        if any(p in detail for p in (
            "timeout", "timed out", "connection refused",
            "connection reset", "connection error",
            "server error", "internal server error",
            "bad gateway", "service unavailable",
            "broken pipe", "reset by peer",
        )):
            return True, "transient"

        # 服务繁忙关键词
        if any(p in detail for p in _BUSY_PATTERNS):
            return True, "busy"

        return False, "generic"

    @staticmethod
    def _extract_status_code(exc: BaseException) -> int | None:
        """尝试提取 HTTP 状态码"""
        for attr in ("status_code", "status"):
            val = getattr(exc, attr, None)
            if isinstance(val, int):
                return val
        response = getattr(exc, "response", None)
        if response is not None:
            return getattr(response, "status_code", None)
        return None

    def _build_delay_ms(self, attempt: int) -> int:
        """计算退避延迟（指数退避，带上限）"""
        backoff = self.base_delay_ms * (2 ** max(0, attempt - 1))
        return min(backoff, self.cap_delay_ms)

    # ════════════════════════════════════════════════════════════════
    # 降级消息
    # ════════════════════════════════════════════════════════════════

    def _build_user_message(self, exc: BaseException, reason: str) -> str:
        """生成面向用户的友好错误消息"""
        if reason == "quota":
            return (
                "抱歉，LLM 服务商的账户配额已用尽或存在计费问题，"
                "暂时无法提供服务。请检查 API 账户后重试。"
            )
        if reason == "auth":
            return (
                "抱歉，LLM 服务认证失败，API Key 可能无效或已过期。"
                "请检查 API 配置后重试。"
            )
        if reason == "rate_limit":
            return (
                "LLM 服务请求过于频繁，已被限流。请稍等片刻再继续对话。"
            )
        # transient / busy / generic
        return (
            "LLM 服务暂时不可用，请稍等片刻再继续对话。"
        )

    def _build_fallback_message(self, exc: BaseException, reason: str) -> AIMessage:
        """构造降级 AIMessage，替代崩溃"""
        return AIMessage(
            content=self._build_user_message(exc, reason),
            additional_kwargs={
                "llm_error_fallback": True,
                "error_type": type(exc).__name__,
                "error_reason": reason,
                "error_detail": str(exc)[:200],
            },
        )

    # ════════════════════════════════════════════════════════════════
    # 模型调用包装（同步）
    # ════════════════════════════════════════════════════════════════

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        # 熔断器检查
        if self._circuit_open():
            logger.warning("⛔ 熔断器断开 (open)，快速失败拒绝请求")
            return self._build_fallback_message(
                Exception("circuit_breaker_open"),
                "circuit_open",
            )

        attempt = 1
        while True:
            try:
                response = handler(request)
                self._record_success()
                return response
            except GraphBubbleUp:
                # LangGraph 控制信号（interrupt/pause/resume）必须透传
                with self._lock:
                    if self._state == "half_open":
                        self._probe_in_flight = False
                raise
            except Exception as exc:
                retriable, reason = self._classify_error(exc)

                if retriable and attempt < self.max_retries:
                    delay_ms = self._build_delay_ms(attempt)
                    logger.warning(
                        "⚠️ LLM 错误 (第 %d/%d 次)，%dms 后重试: %s",
                        attempt,
                        self.max_retries,
                        delay_ms,
                        str(exc)[:120],
                    )
                    time.sleep(delay_ms / 1000)
                    attempt += 1
                    continue

                logger.error(
                    "❌ LLM 调用失败 (尝试 %d 次): %s",
                    attempt,
                    str(exc)[:200],
                    exc_info=exc,
                )
                if retriable:
                    self._record_failure()
                return self._build_fallback_message(exc, reason)

    # ════════════════════════════════════════════════════════════════
    # 模型调用包装（异步）
    # ════════════════════════════════════════════════════════════════

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        if self._circuit_open():
            logger.warning("⛔ 熔断器断开 (open)，快速失败拒绝请求")
            return self._build_fallback_message(
                Exception("circuit_breaker_open"),
                "circuit_open",
            )

        attempt = 1
        while True:
            try:
                response = await handler(request)
                self._record_success()
                return response
            except GraphBubbleUp:
                with self._lock:
                    if self._state == "half_open":
                        self._probe_in_flight = False
                raise
            except Exception as exc:
                retriable, reason = self._classify_error(exc)

                if retriable and attempt < self.max_retries:
                    delay_ms = self._build_delay_ms(attempt)
                    logger.warning(
                        "⚠️ LLM 错误 (第 %d/%d 次)，%dms 后重试: %s",
                        attempt,
                        self.max_retries,
                        delay_ms,
                        str(exc)[:120],
                    )
                    await asyncio.sleep(delay_ms / 1000)
                    attempt += 1
                    continue

                logger.error(
                    "❌ LLM 调用失败 (尝试 %d 次): %s",
                    attempt,
                    str(exc)[:200],
                    exc_info=exc,
                )
                if retriable:
                    self._record_failure()
                return self._build_fallback_message(exc, reason)


def create_llm_error_handling_middleware(
    *,
    max_retries: int = 3,
    base_delay_ms: int = 1000,
    cap_delay_ms: int = 8000,
    circuit_breaker_threshold: int = 3,
    circuit_breaker_timeout: int = 60,
) -> LLMErrorHandlingMiddleware:
    """创建 LLM 错误处理中间件的便捷函数"""
    return LLMErrorHandlingMiddleware(
        max_retries=max_retries,
        base_delay_ms=base_delay_ms,
        cap_delay_ms=cap_delay_ms,
        circuit_breaker_threshold=circuit_breaker_threshold,
        circuit_breaker_timeout=circuit_breaker_timeout,
    )
