"""ApprovalMiddleware - 审批中间件

整合 Provider 评估和 interrupt 人工审批。
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable

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
from src.middlewares.approval.builtin import YamlPolicyProvider


class ApprovalMiddleware(AgentMiddleware):
    """Legacy approval wrapper kept fail-closed around execution-time policy.

    New approval assembly should prefer the official HumanInTheLoopMiddleware.
    This compatibility middleware must never turn an unavailable interrupt
    primitive into authorization to execute a sensitive tool.
    """

    def __init__(
        self,
        provider: ApprovalProvider | None = None,
        *,
        config: Any | None = None,
        fail_closed: bool = True,
        audit_logger: AuditLogger | None = None,
        current_agent: str = "unknown",
    ):
        super().__init__()
        if config is not None and provider is None:
            provider = YamlPolicyProvider(config=config)
            audit_logger = audit_logger or AuditLogger(
                getattr(config, "audit_log_path", "logs/approval_audit.jsonl")
            )
        if provider is None:
            raise ValueError("必须提供 provider 或 config")

        self.provider = provider
        self.fail_closed = fail_closed
        self.audit_logger = audit_logger or AuditLogger()
        self.current_agent = current_agent

    def _get_thread_id(self) -> str:
        try:
            from langgraph.config import get_config
            config = get_config()
            return config.get("configurable", {}).get("thread_id", "unknown")
        except Exception:
            return "unknown"

    def _build_request(self, request: ToolCallRequest) -> ApprovalRequest:
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
        tool_call_id = str(request.tool_call.get("id", ""))
        approval_request = self._build_request(request)

        try:
            decision = await self.provider.aevaluate(approval_request)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            logging.error("Provider 评估失败: %s", exc)
            self.audit_logger.log_provider_error({
                "agent": self.current_agent,
                "tool": approval_request.tool_name,
                "error": str(exc),
                "fallback_used": not self.fail_closed,
            })
            if self.fail_closed:
                decision = ApprovalDecision.blocked(
                    reason_code="provider_error",
                    reason_message=str(exc),
                )
            else:
                return await handler(request)

        self.audit_logger.log_request({
            "agent": self.current_agent,
            "tool": approval_request.tool_name,
            "args": approval_request.tool_input,
            "risk_level": decision.risk_level.value,
            "thread_id": approval_request.thread_id,
            "provider": self.provider.name,
        })

        if not decision.allow and not decision.needs_interrupt:
            self.audit_logger.log_blocked({
                "agent": self.current_agent,
                "tool": approval_request.tool_name,
                "args": approval_request.tool_input,
                "reasons": [reason.message for reason in decision.reasons],
            })
            return ToolMessage(
                content=f"❌ 操作被阻止: {', '.join(reason.message for reason in decision.reasons)}",
                tool_call_id=tool_call_id,
            )

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

        if not HAS_INTERRUPT:
            reason = "interrupt support unavailable; approval-required tool denied"
            logging.error("%s: %s", reason, approval_request.tool_name)
            self.audit_logger.log_blocked({
                "agent": self.current_agent,
                "tool": approval_request.tool_name,
                "args": approval_request.tool_input,
                "reasons": [reason],
            })
            return ToolMessage(
                content=f"❌ 操作被阻止: {reason}",
                tool_call_id=tool_call_id,
                status="error",
            )

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
            "message": decision.interrupt_message
            or f"[{decision.risk_level.value.upper()}风险] 请审批",
        })

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
        return asyncio.run(self.awrap_tool_call(request, handler))
