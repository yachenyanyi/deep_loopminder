"""ApprovalMiddleware - 审批中间件

整合 Provider 评估和 interrupt 人工审批：
1. Provider.evaluate() → ApprovalDecision
2. 如果 needs_interrupt=True → 触发 interrupt
3. 否则根据 allow 执行或拒绝
"""

import json
import logging
from typing import Callable, Awaitable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp

try:
    from langgraph.types import interrupt
    HAS_INTERRUPT = True
except ImportError:
    HAS_INTERRUPT = False

from src.middlewares.approval.provider import ApprovalProvider
from src.middlewares.approval.request import ApprovalRequest
from src.middlewares.approval.decision import ApprovalDecision
from src.middlewares.approval.audit_logger import AuditLogger


class ApprovalMiddleware(AgentMiddleware):
    """审批中间件

    整合 Provider 评估和 interrupt 人工审批。

    流程：
    1. 构建 ApprovalRequest
    2. Provider.evaluate() → ApprovalDecision
    3. 根据 decision 处理：
       - allow=False, needs_interrupt=False → 直接拒绝
       - allow=True, needs_interrupt=False → 直接执行
       - needs_interrupt=True → 触发 interrupt

    Example:
        ```python
        provider = YamlPolicyProvider(config_path="config/approval_policy.yaml")
        middleware = ApprovalMiddleware(
            provider=provider,
            fail_closed=True,
            audit_logger=AuditLogger("logs/approval_audit.jsonl"),
            current_agent="chat_agent",
        )
        ```
    """

    def __init__(
        self,
        provider: ApprovalProvider,
        *,
        fail_closed: bool = True,
        audit_logger: AuditLogger | None = None,
        current_agent: str = "unknown",
    ):
        super().__init__()
        self.provider = provider
        self.fail_closed = fail_closed
        self.audit_logger = audit_logger or AuditLogger()
        self.current_agent = current_agent

    def _get_thread_id(self) -> str:
        """获取当前线程 ID"""
        try:
            from langgraph.config import get_config
            config = get_config()
            return config.get("configurable", {}).get("thread_id", "unknown")
        except Exception:
            return "unknown"

    def _build_request(self, request: ToolCallRequest) -> ApprovalRequest:
        """构建审批请求"""
        return ApprovalRequest(
            tool_name=str(request.tool_call.get("name", "unknown")),
            tool_input=request.tool_call.get("args", {}),
            agent_id=self.current_agent,
            thread_id=self._get_thread_id(),
        )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage]],
    ) -> ToolMessage:
        """包装工具调用（异步版本）"""
        tool_call_id = str(request.tool_call.get("id", ""))

        # 1. 构建请求
        approval_request = self._build_request(request)

        # 2. Provider 评估
        try:
            decision = await self.provider.aevaluate(approval_request)
        except GraphBubbleUp:
            # 保留 LangGraph 控制流信号（interrupt/pause/resume）
            raise
        except Exception as e:
            logging.error(f"Provider 评估失败: {e}")
            self.audit_logger.log_provider_error({
                "agent": self.current_agent,
                "tool": approval_request.tool_name,
                "error": str(e),
                "fallback_used": not self.fail_closed,
            })

            if self.fail_closed:
                # 评估失败时默认拒绝
                decision = ApprovalDecision.blocked(
                    reason_code="provider_error",
                    reason_message=str(e)
                )
            else:
                # 允许通过
                return await handler(request)

        # 3. 记录审计日志
        self.audit_logger.log_request({
            "agent": self.current_agent,
            "tool": approval_request.tool_name,
            "args": approval_request.tool_input,
            "risk_level": decision.risk_level.value,
            "thread_id": approval_request.thread_id,
            "provider": self.provider.name,
        })

        # 4. 黑名单/拒绝：直接返回错误
        if not decision.allow and not decision.needs_interrupt:
            self.audit_logger.log_blocked({
                "agent": self.current_agent,
                "tool": approval_request.tool_name,
                "args": approval_request.tool_input,
                "reasons": [r.message for r in decision.reasons],
            })
            return ToolMessage(
                content=f"❌ 操作被阻止: {', '.join(r.message for r in decision.reasons)}",
                tool_call_id=tool_call_id,
            )

        # 5. 低危/允许：直接执行
        if decision.allow and not decision.needs_interrupt:
            self.audit_logger.log_auto_approved({
                "agent": self.current_agent,
                "tool": approval_request.tool_name,
                "args": approval_request.tool_input,
                "risk_level": decision.risk_level.value,
                "reason": "low_risk_or_auto_mode",
            })
            result = await handler(request)
            self.audit_logger.log_execution({
                "request_id": tool_call_id,
                "success": True,
                "result": result.content if hasattr(result, "content") else str(result),
            })
            return result

        # 6. 需要人工审批：触发 interrupt
        if not HAS_INTERRUPT:
            logging.warning(f"审批中间件缺少 interrupt 支持，自动通过: {approval_request.tool_name}")
            self.audit_logger.log_auto_approved({
                "agent": self.current_agent,
                "tool": approval_request.tool_name,
                "args": approval_request.tool_input,
                "risk_level": decision.risk_level.value,
                "reason": "interrupt_not_available",
            })
            result = await handler(request)
            return result

        self.audit_logger.log_interrupt({
            "request_id": tool_call_id,
            "tool": approval_request.tool_name,
            "risk_level": decision.risk_level.value,
            "message": decision.interrupt_message,
        })

        approval = interrupt({
            "type": "tool_approval",
            "tool_name": approval_request.tool_name,
            "args": approval_request.tool_input,
            "risk_level": decision.risk_level.value,
            "allowed_decisions": decision.allowed_decisions,
            "message": decision.interrupt_message or f"[{decision.risk_level.value.upper()}风险] 请审批",
        })

        # 7. 处理审批决策
        if isinstance(approval, str):
            user_decision = approval
            approver = "user"
            edited_args = None
            reason = None
        elif isinstance(approval, dict):
            user_decision = approval.get("type", "reject")
            approver = approval.get("approver", "user")
            edited_args = approval.get("edited_args")
            reason = approval.get("reason")
        else:
            user_decision = "reject"
            approver = "system"
            edited_args = None
            reason = f"无效的审批格式: {type(approval)}"

        self.audit_logger.log_decision({
            "request_id": tool_call_id,
            "decision": user_decision,
            "approver": approver,
            "edited_args": edited_args if user_decision == "edit" else None,
            "reason": reason,
        })

        if user_decision == "reject":
            return ToolMessage(
                content=f"❌ 用户拒绝了此操作。\n原因: {reason or '未提供'}",
                tool_call_id=tool_call_id,
            )

        if user_decision == "edit" and edited_args:
            request.tool_call["args"] = edited_args

        # 8. 执行工具
        result = await handler(request)
        self.audit_logger.log_execution({
            "request_id": tool_call_id,
            "success": True,
            "result": result.content if hasattr(result, "content") else str(result),
        })
        return result

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        """包装工具调用（同步版本）"""
        import asyncio
        return asyncio.run(self.awrap_tool_call(request, handler))