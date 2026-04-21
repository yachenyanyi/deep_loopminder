"""兼容层 - 保留旧的导入路径

旧代码无需修改，继续使用：
    from src.middlewares.human_approval import HumanApprovalMiddleware, ApprovalConfig

新代码推荐使用：
    from src.middlewares.approval import ApprovalMiddleware, YamlPolicyProvider

这是 deer-flow Guardrail Provider 模式的实现，支持：
- 可插拔的审批策略 Provider
- 配置驱动的动态加载
- fail_closed 模式（评估失败默认拒绝）
- interrupt 人工审批
- 审计日志
"""

# 重导出到新模块（兼容旧代码）
from src.middlewares.approval.middleware import ApprovalMiddleware as HumanApprovalMiddleware
from src.middlewares.approval.builtin import YamlPolicyProvider
from src.middlewares.approval.decision import RiskLevel, ApprovalDecision, ApprovalReason
from src.middlewares.approval.audit_logger import AuditLogger
from src.middlewares.approval.risk_analyzer import RiskAnalyzer, ApprovalConfig
from src.middlewares.approval.config import (
    ApprovalMiddlewareConfig,
    ApprovalProviderConfig,
    load_approval_config,
    resolve_provider,
    create_approval_middleware_from_config,
)


# 便捷函数（兼容旧代码）
def create_approval_middleware(
    config_path: str = "config/approval_policy.yaml",
    current_agent: str = "unknown",
) -> HumanApprovalMiddleware:
    """创建审批中间件的便捷函数（兼容旧代码）

    Args:
        config_path: YAML 配置文件路径
        current_agent: 当前 agent 名称

    Returns:
        HumanApprovalMiddleware: 审批中间件实例
    """
    from src.middlewares.approval.builtin import YamlPolicyProvider
    from src.middlewares.approval.audit_logger import AuditLogger

    provider = YamlPolicyProvider(config_path=config_path)
    audit_logger = AuditLogger()

    return HumanApprovalMiddleware(
        provider=provider,
        audit_logger=audit_logger,
        current_agent=current_agent,
    )


__all__ = [
    # 兼容旧代码
    "HumanApprovalMiddleware",  # ApprovalMiddleware 的别名
    "ApprovalConfig",
    "RiskLevel",
    "RiskAnalyzer",
    "AuditLogger",

    # 新 Provider 模式
    "YamlPolicyProvider",
    "ApprovalDecision",
    "ApprovalReason",

    # 配置
    "ApprovalMiddlewareConfig",
    "ApprovalProviderConfig",
    "load_approval_config",
    "resolve_provider",
    "create_approval_middleware_from_config",

    # 便捷函数
    "create_approval_middleware",
]