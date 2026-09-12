from .summarization import full_featured_summary, role_playing_summary
from .execution import retry_middleware, todo_middleware
from .shell import local_shell_middleware, web_shell_middleware
from .agent import (
    TitleMiddleware,
    create_title_middleware,
    ThreadConfigManager,
    get_thread_config_manager,
    UserConfigurableMiddleware,
    CustomPromptMiddleware,
    create_custom_prompt_middleware,
    SkillsToolMiddleware,
    create_skills_tool_middleware,
)
from .cache import (
    CacheBackend,
    JsonFileCacheBackend,
    PostgresCacheBackend,
    NoneCacheBackend,
    create_cache_backend,
    create_default_cache,
)
from .communication import (
    AgentCommunicationMiddleware,
    Employee,
    create_agent_communication_middleware,
)
from .error import (
    LLMErrorHandlingMiddleware,
    create_llm_error_handling_middleware,
    ToolErrorHandlingMiddleware,
    create_tool_error_handling_middleware,
)
from .logging import (
    LoggingMiddleware,
    LogFormatter,
    TextFormatter,
    JSONFormatter,
    create_logging_middleware,
)
from .tools import (
    BilibiliMiddleware,
    create_bilibili_middleware,
    ErrorBookMiddleware,
    create_error_book_middleware,
    RoadmapMiddleware,
    create_roadmap_middleware,
    QuizMiddleware,
    create_quiz_middleware,
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
    # Agent
    "TitleMiddleware",
    "create_title_middleware",
    "ThreadConfigManager",
    "get_thread_config_manager",
    "UserConfigurableMiddleware",
    "CustomPromptMiddleware",
    "create_custom_prompt_middleware",
    "SkillsToolMiddleware",
    "create_skills_tool_middleware",
    # Cache
    "CacheBackend",
    "JsonFileCacheBackend",
    "PostgresCacheBackend",
    "NoneCacheBackend",
    "create_cache_backend",
    "create_default_cache",
    # Communication
    "AgentCommunicationMiddleware",
    "Employee",
    "create_agent_communication_middleware",
    # Error Handling
    "LLMErrorHandlingMiddleware",
    "create_llm_error_handling_middleware",
    "ToolErrorHandlingMiddleware",
    "create_tool_error_handling_middleware",
    # Logging
    "LoggingMiddleware",
    "LogFormatter",
    "TextFormatter",
    "JSONFormatter",
    "create_logging_middleware",
    # Tools (Bilibili)
    "BilibiliMiddleware",
    "create_bilibili_middleware",
    # Tools (Error Book)
    "ErrorBookMiddleware",
    "create_error_book_middleware",
    # Tools (Roadmap)
    "RoadmapMiddleware",
    "create_roadmap_middleware",
    # Tools (Quiz)
    "QuizMiddleware",
    "create_quiz_middleware",
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
