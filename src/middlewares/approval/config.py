"""配置加载系统

支持：
- Pydantic 模型验证
- YAML 配置文件
- Provider 动态加载
"""

import os
import logging
from importlib import import_module
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from src.middlewares.approval.provider import ApprovalProvider
from src.middlewares.approval.builtin import YamlPolicyProvider


class ApprovalProviderConfig(BaseModel):
    """Provider 配置"""

    use: str = Field(
        default="src.middlewares.approval.builtin:YamlPolicyProvider",
        description="类路径，如 'module:class'"
    )
    config: dict = Field(
        default_factory=dict,
        description="Provider 参数"
    )


class ApprovalMiddlewareConfig(BaseModel):
    """审批中间件配置"""

    enabled: bool = Field(default=True, description="是否启用审批")
    fail_closed: bool = Field(default=True, description="评估失败时默认拒绝")
    audit_log_path: str = Field(default="logs/approval_audit.jsonl", description="审计日志路径")
    provider: ApprovalProviderConfig | None = Field(default=None, description="Provider 配置")


def resolve_provider(provider_config: ApprovalProviderConfig) -> ApprovalProvider:
    """动态加载 Provider

    Args:
        provider_config: Provider 配置，包含 use（类路径）和 config（参数）

    Returns:
        ApprovalProvider: 加载的 Provider 实例

    Example:
        ```yaml
        provider:
          use: "src.middlewares.approval.builtin:YamlPolicyProvider"
          config:
            config_path: "config/approval_policy.yaml"
        ```
    """
    try:
        module_path, class_name = provider_config.use.rsplit(":", 1)
    except ValueError:
        raise ImportError(
            f"{provider_config.use} 不像是类路径。示例: module_path:class_name"
        )

    try:
        module = import_module(module_path)
    except ImportError as err:
        raise ImportError(f"无法导入模块 {module_path}: {err}")

    try:
        provider_class = getattr(module, class_name)
    except AttributeError:
        raise ImportError(f"模块 {module_path} 没有 {class_name} 属性/类")

    # 创建实例
    return provider_class(**provider_config.config)


def load_approval_config(config_path: str = "config/approval.yaml") -> ApprovalMiddlewareConfig:
    """从 YAML 文件加载审批配置

    Args:
        config_path: YAML 配置文件路径

    Returns:
        ApprovalMiddlewareConfig: 配置对象
    """
    path = Path(config_path)
    if not path.exists():
        logging.warning(f"审批配置文件不存在: {config_path}，使用默认配置")
        return ApprovalMiddlewareConfig()

    try:
        with open(path, encoding="utf-8") as f:
            yaml_config = yaml.safe_load(f) or {}

        # 解析环境变量
        yaml_config = resolve_env_variables(yaml_config)

        return ApprovalMiddlewareConfig.model_validate(yaml_config)
    except Exception as e:
        logging.error(f"加载审批配置失败: {e}，使用默认配置")
        return ApprovalMiddlewareConfig()


def resolve_env_variables(config: dict) -> dict:
    """递归解析环境变量

    支持 $ENV_VAR 格式的环境变量引用。
    """
    import os

    def resolve(value):
        if isinstance(value, str) and value.startswith("$"):
            env_var = value[1:]
            env_value = os.getenv(env_var)
            if env_value is None:
                raise ValueError(f"环境变量 {env_var} 未设置")
            return env_value
        elif isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    return resolve(config)


def create_approval_middleware_from_config(
    config_path: str = "config/approval.yaml",
    current_agent: str = "unknown",
) -> "ApprovalMiddleware":
    """从配置文件创建审批中间件

    Args:
        config_path: YAML 配置文件路径
        current_agent: 当前代理名称

    Returns:
        ApprovalMiddleware: 审批中间件实例
    """
    from src.middlewares.approval.middleware import ApprovalMiddleware
    from src.middlewares.approval.audit_logger import AuditLogger

    config = load_approval_config(config_path)

    if not config.enabled:
        # 返回一个空中间件（不拦截）
        return None

    # 加载 Provider
    if config.provider:
        provider = resolve_provider(config.provider)
    else:
        provider = YamlPolicyProvider()

    # 创建审计日志
    audit_logger = AuditLogger(config.audit_log_path)

    return ApprovalMiddleware(
        provider=provider,
        fail_closed=config.fail_closed,
        audit_logger=audit_logger,
        current_agent=current_agent,
    )