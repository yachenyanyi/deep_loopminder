"""Approval 模块 - Provider 模式审批系统

借鉴 deer-flow 的 Guardrail Provider 设计模式。
支持可插拔的审批策略、远程审批服务、配置驱动加载。

使用方法：
```python
# 方式 1：直接使用 Provider
from src.middlewares.approval import ApprovalMiddleware, YamlPolicyProvider

provider = YamlPolicyProvider(config_path="config/approval_policy.yaml")
middleware = ApprovalMiddleware(provider=provider, current_agent="chat_agent")

# 方式 2：从配置文件加载
from src.middlewares.approval import create_approval_middleware_from_config

middleware = create_approval_middleware_from_config(
    config_path="config/approval.yaml",
    current_agent="chat_agent",
)

# 方式 3：兼容旧代码
from src.middlewares.human_approval import HumanApprovalMiddleware, ApprovalConfig

middleware = HumanApprovalMiddleware(
    config=ApprovalConfig.from_yaml("config/approval_policy.yaml"),
    current_agent="chat_agent",
)
```
"""

# Provider 接口
from src.middlewares.approval.provider import ApprovalProvider

# Request/Decision
from src.middlewares.approval.request import ApprovalRequest
from src.middlewares.approval.decision import ApprovalDecision, ApprovalReason, RiskLevel

# Middleware
from src.middlewares.approval.middleware import ApprovalMiddleware

# 内置 Provider
from src.middlewares.approval.builtin import (
    YamlPolicyProvider,
    AllowlistProvider,
    RemoteApprovalProvider,
)

# 审计日志
from src.middlewares.approval.audit_logger import AuditLogger

# 配置
from src.middlewares.approval.config import (
    ApprovalMiddlewareConfig,
    ApprovalProviderConfig,
    load_approval_config,
    resolve_provider,
    create_approval_middleware_from_config,
)

# 风险分析（兼容）
from src.middlewares.approval.risk_analyzer import RiskAnalyzer, ApprovalConfig


__all__ = [
    # Provider 接口
    "ApprovalProvider",

    # Request/Decision
    "ApprovalRequest",
    "ApprovalDecision",
    "ApprovalReason",
    "RiskLevel",

    # Middleware
    "ApprovalMiddleware",

    # 内置 Provider
    "YamlPolicyProvider",
    "AllowlistProvider",
    "RemoteApprovalProvider",

    # 审计日志
    "AuditLogger",

    # 配置
    "ApprovalMiddlewareConfig",
    "ApprovalProviderConfig",
    "load_approval_config",
    "resolve_provider",
    "create_approval_middleware_from_config",

    # 兼容旧代码
    "RiskAnalyzer",
    "ApprovalConfig",
]