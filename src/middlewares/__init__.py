from .summarization import full_featured_summary, role_playing_summary
from .execution import retry_middleware, todo_middleware
from .shell import local_shell_middleware, web_shell_middleware
from .agent_communication import (
    AgentCommunicationMiddleware,
    Employee,
    create_agent_communication_middleware,
)
from .logging import (
    LoggingMiddleware,
    LogFormatter,
    TextFormatter,
    JSONFormatter,
    create_logging_middleware,
)

# Approval 模块（Provider 模式）
from .approval import (
    ApprovalProvider,
    ApprovalRequest,
    ApprovalDecision,
    ApprovalReason,
    RiskLevel,
    ApprovalMiddleware,
    YamlPolicyProvider,
    AllowlistProvider,
    RemoteApprovalProvider,
    AuditLogger,
    ApprovalMiddlewareConfig,
    ApprovalProviderConfig,
    load_approval_config,
    resolve_provider,
    create_approval_middleware_from_config,
)

# 兼容层（旧代码）
from .human_approval import (
    HumanApprovalMiddleware,
    ApprovalConfig,
    RiskAnalyzer,
    create_approval_middleware,
)


__all__ = [
    "full_featured_summary",
    "role_playing_summary",
    "retry_middleware",
    "todo_middleware",
    "local_shell_middleware",
    "web_shell_middleware",
    # Agent Communication
    "AgentCommunicationMiddleware",
    "Employee",
    "create_agent_communication_middleware",
    # Logging
    "LoggingMiddleware",
    "LogFormatter",
    "TextFormatter",
    "JSONFormatter",
    "create_logging_middleware",
    # Approval (Provider 模式)
    "ApprovalProvider",
    "ApprovalRequest",
    "ApprovalDecision",
    "ApprovalReason",
    "RiskLevel",
    "ApprovalMiddleware",
    "YamlPolicyProvider",
    "AllowlistProvider",
    "RemoteApprovalProvider",
    "AuditLogger",
    "ApprovalMiddlewareConfig",
    "ApprovalProviderConfig",
    "load_approval_config",
    "resolve_provider",
    "create_approval_middleware_from_config",
    # 兼容层
    "HumanApprovalMiddleware",
    "ApprovalConfig",
    "RiskAnalyzer",
    "create_approval_middleware",
]
