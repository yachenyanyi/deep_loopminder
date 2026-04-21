"""ApprovalProvider Protocol - 审批策略接口

借鉴 deer-flow 的 Guardrail Provider 设计模式。
实现此接口的类可以作为审批策略使用。

支持的 Provider 类型：
- YamlPolicyProvider：基于 YAML 配置的风险策略
- AllowlistProvider：简单的白名单/黑名单策略
- RemoteApprovalProvider：调用远程审批 API
"""

from typing import Protocol, runtime_checkable

from src.middlewares.approval.request import ApprovalRequest
from src.middlewares.approval.decision import ApprovalDecision


@runtime_checkable
class ApprovalProvider(Protocol):
    """审批策略 Provider 接口

    实现此接口的类可以作为审批策略使用。
    支持：
    - 本地策略（YAML 配置）
    - 远程服务（调用外部审批 API）
    - AI 风险评估（LLM 判断）

    Example:
        ```python
        class MyProvider:
            name = "my_provider"

            def evaluate(self, request: ApprovalRequest) -> ApprovalDecision:
                # 实现评估逻辑
                if request.tool_name == "dangerous_tool":
                    return ApprovalDecision(
                        allow=False,
                        needs_interrupt=False,
                        reasons=[ApprovalReason(code="dangerous", message="禁止")]
                    )
                return ApprovalDecision(allow=True, needs_interrupt=False)

            async def aevaluate(self, request: ApprovalRequest) -> ApprovalDecision:
                return self.evaluate(request)
        ```
    """

    name: str  # Provider 名称

    def evaluate(self, request: ApprovalRequest) -> ApprovalDecision:
        """同步评估工具调用

        Args:
            request: 审批请求，包含工具名称、参数、代理ID等

        Returns:
            ApprovalDecision: 包含 allow, needs_interrupt, risk_level 等
        """
        ...

    async def aevaluate(self, request: ApprovalRequest) -> ApprovalDecision:
        """异步评估工具调用

        Args:
            request: 审批请求

        Returns:
            ApprovalDecision: 审批决策
        """
        ...