import asyncio
from types import SimpleNamespace

from langchain_core.messages import ToolMessage

import src.middlewares.approval.middleware as approval_module
from src.middlewares.approval.decision import ApprovalDecision, RiskLevel
from src.middlewares.approval.middleware import ApprovalMiddleware


class ApprovalRequiredProvider:
    name = "approval-required"

    async def aevaluate(self, request):
        return ApprovalDecision.needs_approval(
            RiskLevel.HIGH,
            "human approval required",
        )


class NoopAuditLogger:
    def __getattr__(self, name):
        return lambda payload: None


def test_missing_interrupt_fails_closed(monkeypatch):
    """Approval-required work must never execute when HITL cannot interrupt."""

    async def scenario():
        monkeypatch.setattr(approval_module, "HAS_INTERRUPT", False)
        middleware = ApprovalMiddleware(
            provider=ApprovalRequiredProvider(),
            audit_logger=NoopAuditLogger(),
        )
        request = SimpleNamespace(
            tool_call={"id": "call-1", "name": "dangerous_write", "args": {}}
        )
        executed = False

        async def handler(_request):
            nonlocal executed
            executed = True
            return ToolMessage(content="executed", tool_call_id="call-1")

        result = await middleware.awrap_tool_call(request, handler)

        assert executed is False
        assert result.status == "error"
        assert "interrupt support unavailable" in str(result.content)

    asyncio.run(scenario())
