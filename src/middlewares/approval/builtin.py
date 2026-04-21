"""内置 Provider 实现

包含：
- YamlPolicyProvider：基于 YAML 配置的风险策略
- AllowlistProvider：简单的白名单/黑名单策略
- RemoteApprovalProvider：调用远程审批 API
"""

import logging
from typing import Any

from src.middlewares.approval.provider import ApprovalProvider
from src.middlewares.approval.request import ApprovalRequest
from src.middlewares.approval.decision import ApprovalDecision, ApprovalReason, RiskLevel
from src.middlewares.approval.risk_analyzer import RiskAnalyzer, ApprovalConfig


class YamlPolicyProvider:
    """基于 YAML 配置的风险策略 Provider

    这是默认的 Provider，实现当前的风险分析逻辑。
    支持全自动模式（开发/紧急模式）。
    """

    name = "yaml_policy"

    def __init__(
        self,
        config: ApprovalConfig | None = None,
        config_path: str | None = None,
        auto_mode_enabled: bool = False,
    ):
        if config is not None:
            self.config = config
        elif config_path is not None:
            self.config = self._load_config(config_path)
        else:
            self.config = ApprovalConfig(auto_mode_enabled=auto_mode_enabled)

        self.risk_analyzer = RiskAnalyzer(self.config)

    def _load_config(self, config_path: str) -> ApprovalConfig:
        """从 YAML 文件加载配置"""
        import yaml
        from pathlib import Path

        path = Path(config_path)
        if not path.exists():
            logging.warning(f"审批配置文件不存在: {config_path}，使用默认配置")
            return ApprovalConfig()

        try:
            with open(path, encoding="utf-8") as f:
                yaml_config = yaml.safe_load(f) or {}

            return ApprovalConfig(
                blacklist=yaml_config.get("blacklist", ApprovalConfig.DEFAULT_BLACKLIST),
                high_risk=yaml_config.get("high_risk", ApprovalConfig.DEFAULT_HIGH_RISK),
                medium_risk=yaml_config.get("medium_risk", ApprovalConfig.DEFAULT_MEDIUM_RISK),
                whitelist=yaml_config.get("whitelist", ApprovalConfig.DEFAULT_WHITELIST),
                tool_config=yaml_config.get("tool_config", {}),
                auto_mode_enabled=yaml_config.get("auto_mode", {}).get("enabled", False),
            )
        except Exception as e:
            logging.error(f"加载审批配置失败: {e}，使用默认配置")
            return ApprovalConfig()

    def evaluate(self, request: ApprovalRequest) -> ApprovalDecision:
        """评估工具调用"""
        risk_level = self.risk_analyzer.analyze(request.tool_name, request.tool_input)

        # 黑名单：绝对禁止
        if risk_level == RiskLevel.BLOCKED:
            return ApprovalDecision.blocked(
                reason_code="blacklist_command",
                reason_message=f"黑名单命令: {request.tool_input.get('command', 'unknown')}"
            )

        # 全自动模式：自动通过（黑名单已在上一步阻止）
        if self.config.auto_mode_enabled:
            return ApprovalDecision.allowed(risk_level)

        # 低危：直接执行
        if risk_level == RiskLevel.LOW:
            return ApprovalDecision.allowed(risk_level)

        # 高危/中危：需要 interrupt
        return ApprovalDecision.needs_approval(
            risk_level=risk_level,
            message=f"[{risk_level.value.upper()}风险] 请审批工具调用:\n工具: {request.tool_name}\n参数: {str(request.tool_input)[:500]}",
            allowed_decisions=self.config.get_allowed_decisions(request.tool_name)
        )

    async def aevaluate(self, request: ApprovalRequest) -> ApprovalDecision:
        """异步评估"""
        return self.evaluate(request)


class AllowlistProvider:
    """简单的白名单/黑名单 Provider

    只允许/拒绝特定工具执行，不做风险分析。
    """

    name = "allowlist"

    def __init__(
        self,
        allowed_tools: list[str] | None = None,
        denied_tools: list[str] | None = None,
    ):
        self._allowed = set(allowed_tools) if allowed_tools else None
        self._denied = set(denied_tools) if denied_tools else set()

    def evaluate(self, request: ApprovalRequest) -> ApprovalDecision:
        """评估工具调用"""
        # 白名单检查
        if self._allowed is not None and request.tool_name not in self._allowed:
            return ApprovalDecision.blocked(
                reason_code="tool_not_allowed",
                reason_message=f"工具 '{request.tool_name}' 不在白名单中"
            )

        # 黑名单检查
        if request.tool_name in self._denied:
            return ApprovalDecision.blocked(
                reason_code="tool_denied",
                reason_message=f"工具 '{request.tool_name}' 在黑名单中"
            )

        # 允许执行
        return ApprovalDecision.allowed()

    async def aevaluate(self, request: ApprovalRequest) -> ApprovalDecision:
        """异步评估"""
        return self.evaluate(request)


class RemoteApprovalProvider:
    """远程审批服务 Provider

    调用外部审批 API 进行评估。适用于：
    - 企业审批系统集成
    - 多级审批流程
    - 审批历史管理
    """

    name = "remote_approval"

    def __init__(
        self,
        api_url: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        fallback_local: bool = True,
        fallback_provider: ApprovalProvider | None = None,
    ):
        self.api_url = api_url
        self.api_key = api_key
        self.timeout = timeout
        self.fallback_local = fallback_local
        self.fallback_provider = fallback_provider or YamlPolicyProvider()

    def evaluate(self, request: ApprovalRequest) -> ApprovalDecision:
        """同步评估（使用 httpx 同步客户端）"""
        try:
            import httpx

            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            response = httpx.post(
                self.api_url,
                json={
                    "tool_name": request.tool_name,
                    "tool_input": request.tool_input,
                    "agent_id": request.agent_id,
                    "thread_id": request.thread_id,
                },
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()

            return ApprovalDecision(
                allow=data.get("allow", False),
                needs_interrupt=data.get("needs_interrupt", False),
                risk_level=RiskLevel(data.get("risk_level", "low")),
                reasons=[
                    ApprovalReason(code=r.get("code", ""), message=r.get("message", ""))
                    for r in data.get("reasons", [])
                ],
                policy_id=data.get("policy_id"),
                interrupt_message=data.get("interrupt_message"),
                allowed_decisions=data.get("allowed_decisions", ["approve", "edit", "reject"]),
            )

        except Exception as e:
            logging.error(f"远程审批 API 调用失败: {e}")

            # 回退到本地策略
            if self.fallback_local:
                return self.fallback_provider.evaluate(request)

            # 不回退：返回拒绝
            return ApprovalDecision.blocked(
                reason_code="remote_api_error",
                reason_message=str(e)
            )

    async def aevaluate(self, request: ApprovalRequest) -> ApprovalDecision:
        """异步评估（使用 httpx 异步客户端）"""
        try:
            import httpx

            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.api_url,
                    json={
                        "tool_name": request.tool_name,
                        "tool_input": request.tool_input,
                        "agent_id": request.agent_id,
                        "thread_id": request.thread_id,
                    },
                    headers=headers,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()

                return ApprovalDecision(
                    allow=data.get("allow", False),
                    needs_interrupt=data.get("needs_interrupt", False),
                    risk_level=RiskLevel(data.get("risk_level", "low")),
                    reasons=[
                        ApprovalReason(code=r.get("code", ""), message=r.get("message", ""))
                        for r in data.get("reasons", [])
                    ],
                    policy_id=data.get("policy_id"),
                    interrupt_message=data.get("interrupt_message"),
                    allowed_decisions=data.get("allowed_decisions", ["approve", "edit", "reject"]),
                )

        except Exception as e:
            logging.error(f"远程审批 API 调用失败: {e}")

            # 回退到本地策略
            if self.fallback_local:
                return await self.fallback_provider.aevaluate(request)

            # 不回退：返回拒绝
            return ApprovalDecision.blocked(
                reason_code="remote_api_error",
                reason_message=str(e)
            )