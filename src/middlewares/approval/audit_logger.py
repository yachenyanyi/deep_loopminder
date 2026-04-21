"""AuditLogger - 审批审计日志

记录所有审批操作，包括：
- 审批请求
- 审批决策
- 执行结果
- 自动审批（全自动模式）
- 阻止的操作（黑名单）
"""

import json
import logging
from datetime import datetime
from pathlib import Path


class AuditLogger:
    """审批审计日志

    记录所有审批操作到 JSONL 文件。

    注意：日志目录应该在模块加载时预先创建，
    避免在异步上下文中执行阻塞操作。
    """

    def __init__(self, log_path: str = "logs/approval_audit.jsonl"):
        self.log_path = Path(log_path)

        # 不在 __init__ 中创建目录，依赖外部预先创建
        self.logger = logging.getLogger("approval_audit")
        self.logger.setLevel(logging.INFO)

        # 添加文件处理器
        if not self.logger.handlers:
            handler = logging.FileHandler(self.log_path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)

    def _write(self, entry: dict):
        """写入日志条目"""
        self.logger.info(json.dumps(entry, ensure_ascii=False))

    def log_request(self, event: dict):
        """记录审批请求"""
        self._write({
            "timestamp": datetime.now().isoformat(),
            "event_type": "approval_request",
            "agent": event.get("agent"),
            "tool": event.get("tool"),
            "args": event.get("args"),
            "risk_level": event.get("risk_level"),
            "thread_id": event.get("thread_id"),
            "provider": event.get("provider"),
        })

    def log_blocked(self, event: dict):
        """记录阻止的操作"""
        self._write({
            "timestamp": datetime.now().isoformat(),
            "event_type": "blocked",
            "agent": event.get("agent"),
            "tool": event.get("tool"),
            "args": event.get("args"),
            "reasons": event.get("reasons"),
        })

    def log_interrupt(self, event: dict):
        """记录触发 interrupt"""
        self._write({
            "timestamp": datetime.now().isoformat(),
            "event_type": "interrupt_triggered",
            "request_id": event.get("request_id"),
            "tool": event.get("tool"),
            "risk_level": event.get("risk_level"),
            "message": event.get("message"),
        })

    def log_decision(self, event: dict):
        """记录审批决策"""
        self._write({
            "timestamp": datetime.now().isoformat(),
            "event_type": "approval_decision",
            "request_id": event.get("request_id"),
            "decision": event.get("decision"),
            "approver": event.get("approver"),
            "edited_args": event.get("edited_args") if event.get("decision") == "edit" else None,
            "reason": event.get("reason"),
        })

    def log_auto_approved(self, event: dict):
        """记录自动审批"""
        self._write({
            "timestamp": datetime.now().isoformat(),
            "event_type": "auto_approved",
            "agent": event.get("agent"),
            "tool": event.get("tool"),
            "args": event.get("args"),
            "risk_level": event.get("risk_level"),
            "reason": event.get("reason"),
        })

    def log_execution(self, event: dict):
        """记录执行结果"""
        self._write({
            "timestamp": datetime.now().isoformat(),
            "event_type": "execution_result",
            "request_id": event.get("request_id"),
            "success": event.get("success"),
            "result_preview": str(event.get("result", ""))[:200],
        })

    def log_provider_error(self, event: dict):
        """记录 Provider 评估错误"""
        self._write({
            "timestamp": datetime.now().isoformat(),
            "event_type": "provider_error",
            "agent": event.get("agent"),
            "tool": event.get("tool"),
            "error": event.get("error"),
            "fallback_used": event.get("fallback_used"),
        })