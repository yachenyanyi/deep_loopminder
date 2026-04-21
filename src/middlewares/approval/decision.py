"""ApprovalDecision - 审批决策数据类

包含 Provider 评估后的决策结果。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RiskLevel(Enum):
    """风险等级枚举

    BLOCKED: 黑名单 - 绝对禁止，不执行也不需要审批
    HIGH: 高危 - 需要 interrupt 人工审批
    MEDIUM: 中危 - 需要 interrupt 人工审批
    LOW: 低危 - 直接执行，不需要审批
    """
    BLOCKED = "blocked"  # 黑名单：绝对禁止
    HIGH = "high"        # 高危：需要 interrupt
    MEDIUM = "medium"    # 中危：需要 interrupt
    LOW = "low"          # 低危：直接执行


@dataclass
class ApprovalReason:
    """审批决策原因

    用于结构化描述为什么做出某个决策。

    Attributes:
        code: 原因代码，如 "blacklist_command", "high_risk_operation"
        message: 原因描述
    """
    code: str           # 原因代码
    message: str = ""   # 原因描述


@dataclass
class ApprovalDecision:
    """审批决策

    Provider.evaluate() 返回的决策结果。

    Attributes:
        allow: 是否允许执行
        needs_interrupt: 是否需要人工审批（触发 interrupt）
        risk_level: 风险等级
        reasons: 决策原因列表
        policy_id: 策略ID（可选）
        metadata: 额外元数据

        allowed_decisions: interrupt 允许的决策类型
        interrupt_message: interrupt 显示的消息
    """

    allow: bool                              # 是否允许执行
    needs_interrupt: bool = False            # 是否需要人工审批（interrupt）
    risk_level: RiskLevel = RiskLevel.LOW    # 风险等级
    reasons: list[ApprovalReason] = field(default_factory=list)
    policy_id: str | None = None             # 策略 ID
    metadata: dict[str, Any] = field(default_factory=dict)

    # interrupt 相关配置
    allowed_decisions: list[str] = field(default_factory=lambda: ["approve", "edit", "reject"])
    interrupt_message: str | None = None     # interrupt 显示的消息

    @classmethod
    def allowed(cls, risk_level: RiskLevel = RiskLevel.LOW) -> "ApprovalDecision":
        """创建允许执行的决策"""
        return cls(allow=True, needs_interrupt=False, risk_level=risk_level)

    @classmethod
    def blocked(cls, reason_code: str = "blocked", reason_message: str = "") -> "ApprovalDecision":
        """创建黑名单阻止的决策"""
        return cls(
            allow=False,
            needs_interrupt=False,
            risk_level=RiskLevel.BLOCKED,
            reasons=[ApprovalReason(code=reason_code, message=reason_message)]
        )

    @classmethod
    def needs_approval(
        cls,
        risk_level: RiskLevel,
        message: str,
        allowed_decisions: list[str] = None
    ) -> "ApprovalDecision":
        """创建需要人工审批的决策"""
        return cls(
            allow=False,
            needs_interrupt=True,
            risk_level=risk_level,
            interrupt_message=message,
            allowed_decisions=allowed_decisions or ["approve", "edit", "reject"]
        )