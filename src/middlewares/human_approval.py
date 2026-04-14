"""
企业级人工审批中间件

为敏感操作提供人工审批机制，符合企业安全规范。

核心功能：
- 风险分级：黑名单/高危/中危/低危
- YAML 配置：支持动态加载审批策略
- 审计日志：记录所有审批操作
- 全自动模式：开发/紧急模式自动通过

使用方法：
```python
from src.middlewares.human_approval import HumanApprovalMiddleware, ApprovalConfig

# 从 YAML 文件加载配置
config = ApprovalConfig.from_yaml("config/approval_policy.yaml")

# 创建中间件
approval_middleware = HumanApprovalMiddleware(
    config=config,
    current_agent="chat_agent",
)

# 配置到 agent
middleware=[logging_middleware, approval_middleware, ...]
```
"""

import os
import json
import logging
import asyncio
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Awaitable

import yaml

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

try:
    from langgraph.types import interrupt
    HAS_INTERRUPT = True
except ImportError:
    HAS_INTERRUPT = False


class RiskLevel(Enum):
    """风险等级枚举"""
    BLOCKED = "blocked"  # 黑名单：绝对禁止
    HIGH = "high"        # 高危：必须审批
    MEDIUM = "medium"    # 中危：需要审批
    LOW = "low"          # 低危：不需审批


class ApprovalConfig:
    """审批配置类

    支持：
    - 默认配置（代码内置）
    - YAML 配置文件加载
    - 环境变量控制全自动模式
    """

    # 默认黑名单（绝对禁止）
    DEFAULT_BLACKLIST = [
        "rm -rf /",
        "rm -rf /*",
        "mkfs",
        "shutdown",
        "reboot",
        ":(){ :|:& };:",
    ]

    # 默认高危命令
    DEFAULT_HIGH_RISK = [
        "rm",
        "sudo",
        "DROP",
        "DELETE",
        "chmod 777",
        "kill -9",
    ]

    # 默认中危命令
    DEFAULT_MEDIUM_RISK = [
        "pip install",
        "npm install",
        "git push",
    ]

    # 默认白名单
    DEFAULT_WHITELIST = [
        "ls",
        "cat",
        "grep",
        "head",
        "tail",
        "echo",
        "pwd",
        "git status",
        "git log",
    ]

    def __init__(
        self,
        blacklist: list[str] | None = None,
        high_risk: list[str] | None = None,
        medium_risk: list[str] | None = None,
        whitelist: list[str] | None = None,
        tool_config: dict | None = None,
        audit_log_path: str = "logs/approval_audit.jsonl",
        auto_mode_enabled: bool = False,
    ):
        self.blacklist = blacklist or self.DEFAULT_BLACKLIST
        self.high_risk = high_risk or self.DEFAULT_HIGH_RISK
        self.medium_risk = medium_risk or self.DEFAULT_MEDIUM_RISK
        self.whitelist = whitelist or self.DEFAULT_WHITELIST
        self.tool_config = tool_config or {}
        self.audit_log_path = audit_log_path
        self.auto_mode_enabled = auto_mode_enabled

        # 从环境变量读取全自动模式（优先级低于配置文件）
        if os.getenv("APPROVAL_AUTO_MODE", "false").lower() == "true":
            self.auto_mode_enabled = True

    @classmethod
    def from_yaml(cls, config_path: str = "config/approval_policy.yaml") -> "ApprovalConfig":
        """从 YAML 文件加载配置"""
        path = Path(config_path)

        if not path.exists():
            logging.warning(f"审批配置文件不存在: {config_path}，使用默认配置")
            return cls()

        try:
            with open(path, encoding="utf-8") as f:
                yaml_config = yaml.safe_load(f)

            return cls(
                blacklist=yaml_config.get("blacklist", cls.DEFAULT_BLACKLIST),
                high_risk=yaml_config.get("high_risk", cls.DEFAULT_HIGH_RISK),
                medium_risk=yaml_config.get("medium_risk", cls.DEFAULT_MEDIUM_RISK),
                whitelist=yaml_config.get("whitelist", cls.DEFAULT_WHITELIST),
                tool_config=yaml_config.get("tool_config", {}),
                audit_log_path=yaml_config.get("audit", {}).get("log_path", "logs/approval_audit.jsonl"),
                auto_mode_enabled=yaml_config.get("auto_mode", {}).get("enabled", False),
            )
        except Exception as e:
            logging.error(f"加载审批配置失败: {e}，使用默认配置")
            return cls()

    def should_auto_approve(self, risk_level: RiskLevel) -> bool:
        """判断是否自动通过（全自动模式）"""
        if self.auto_mode_enabled:
            # 黑名单始终阻止（安全底线）
            if risk_level == RiskLevel.BLOCKED:
                return False
            # 其他风险等级自动通过
            return True
        return False

    def is_tool_enabled(self, tool_name: str) -> bool:
        """检查工具是否启用审批"""
        tool_cfg = self.tool_config.get(tool_name, {})
        if isinstance(tool_cfg, bool):
            return tool_cfg
        return tool_cfg.get("enabled", True)

    def get_allowed_decisions(self, tool_name: str) -> list[str]:
        """获取工具允许的决策类型"""
        tool_cfg = self.tool_config.get(tool_name, {})
        if isinstance(tool_cfg, bool):
            return ["approve", "edit", "reject"]
        return tool_cfg.get("allowed_decisions", ["approve", "edit", "reject"])


class RiskAnalyzer:
    """风险分析器

    分析工具调用的风险等级。
    """

    def __init__(self, config: ApprovalConfig):
        self.config = config

    def analyze(self, tool_name: str, args: dict) -> RiskLevel:
        """分析工具调用的风险等级

        Args:
            tool_name: 工具名称
            args: 工具参数

        Returns:
            RiskLevel: 风险等级
        """
        # 检查工具级配置是否禁用审批
        if not self.config.is_tool_enabled(tool_name):
            return RiskLevel.LOW

        # 根据工具类型分析
        if tool_name == "run_shell_command":
            return self._analyze_shell_command(args.get("command", ""))
        elif tool_name == "call_tool":
            return self._analyze_mcp_tool(args.get("tool_name", ""), args.get("args", {}))
        else:
            return self._analyze_generic_tool(tool_name)

    def _analyze_shell_command(self, command: str) -> RiskLevel:
        """分析 shell 命令的风险"""
        command_lower = command.lower()

        # 1. 黑名单检查（最高优先级）
        for pattern in self.config.blacklist:
            if pattern.lower() in command_lower:
                return RiskLevel.BLOCKED

        # 2. 高危检查
        for pattern in self.config.high_risk:
            if pattern.lower() in command_lower:
                return RiskLevel.HIGH

        # 3. 中危检查
        for pattern in self.config.medium_risk:
            if pattern.lower() in command_lower:
                return RiskLevel.MEDIUM

        # 4. 白名单检查
        for pattern in self.config.whitelist:
            if command_lower.startswith(pattern.lower()):
                return RiskLevel.LOW

        # 5. 默认：低危
        return RiskLevel.LOW

    def _analyze_mcp_tool(self, tool_name: str, args: dict) -> RiskLevel:
        """分析 MCP 工具的风险"""
        # MCP 工具默认为中危（需要审批）
        # 因为 MCP 工具可能涉及网络操作、数据库操作等
        return RiskLevel.MEDIUM

    def _analyze_generic_tool(self, tool_name: str) -> RiskLevel:
        """分析通用工具的风险"""
        # 协作类工具不需要审批
        if tool_name in ["collaborate", "check_colleague", "get_thread_info", "list_resources"]:
            return RiskLevel.LOW

        # 其他工具默认低危
        return RiskLevel.LOW


class AuditLogger:
    """审批审计日志

    记录所有审批操作，包括：
    - 审批请求
    - 审批决策
    - 执行结果
    - 自动审批（全自动模式）

    注意：日志目录应该在模块加载时预先创建（如 collaborative_agents.py 中的 LOGS_DIR），
    避免在异步上下文中执行阻塞操作。
    """

    def __init__(self, log_path: str = "logs/approval_audit.jsonl"):
        self.log_path = Path(log_path)

        # 不在 __init__ 中创建目录，依赖外部预先创建
        # 如果目录不存在，logging.FileHandler 会自动创建
        # 但为了安全，建议在模块加载时预先创建（如 collaborative_agents.py）

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

    def log_approval_request(self, event: dict):
        """记录审批请求"""
        self._write({
            "timestamp": datetime.now().isoformat(),
            "event_type": "approval_request",
            "agent": event.get("agent"),
            "tool": event.get("tool"),
            "args": event.get("args"),
            "risk_level": event.get("risk_level"),
            "thread_id": event.get("thread_id"),
            "auto_mode": event.get("auto_mode", False),
        })

    def log_approval_decision(self, event: dict):
        """记录审批决策"""
        self._write({
            "timestamp": datetime.now().isoformat(),
            "event_type": "approval_decision",
            "request_id": event.get("request_id"),
            "decision": event.get("decision"),
            "approver": event.get("approver"),
            "edited_args": event.get("edited_args"),
            "reason": event.get("reason"),
        })

    def log_auto_approved(self, event: dict):
        """记录自动审批（全自动模式）"""
        self._write({
            "timestamp": datetime.now().isoformat(),
            "event_type": "auto_approved",
            "agent": event.get("agent"),
            "tool": event.get("tool"),
            "args": event.get("args"),
            "risk_level": event.get("risk_level"),
            "reason": "auto_mode_enabled",
        })

    def log_blocked(self, event: dict):
        """记录阻止的操作（黑名单）"""
        self._write({
            "timestamp": datetime.now().isoformat(),
            "event_type": "blocked",
            "agent": event.get("agent"),
            "tool": event.get("tool"),
            "args": event.get("args"),
            "reason": "blacklist_command",
        })

    def log_execution_result(self, event: dict):
        """记录执行结果"""
        self._write({
            "timestamp": datetime.now().isoformat(),
            "event_type": "execution_result",
            "request_id": event.get("request_id"),
            "success": event.get("success"),
            "result_preview": str(event.get("result", ""))[:200],
        })


class HumanApprovalMiddleware(AgentMiddleware):
    """企业级人工审批中间件

    功能：
    1. 风险分级：黑名单/高危/中危/低危
    2. 审批流程：approve/edit/reject
    3. 审计日志：完整记录所有审批操作
    4. 全自动模式：开发/紧急模式自动通过

    使用方法：
    ```python
    approval_middleware = HumanApprovalMiddleware(
        config=ApprovalConfig.from_yaml("config/approval_policy.yaml"),
        current_agent="chat_agent",
    )
    ```
    """

    def __init__(
        self,
        config: ApprovalConfig | None = None,
        audit_logger: AuditLogger | None = None,
        risk_analyzer: RiskAnalyzer | None = None,
        current_agent: str = "unknown",
    ):
        super().__init__()
        self.config = config or ApprovalConfig()
        self.audit_logger = audit_logger or AuditLogger(self.config.audit_log_path)
        self.risk_analyzer = risk_analyzer or RiskAnalyzer(self.config)
        self.current_agent = current_agent

    def _get_thread_id(self) -> str:
        """获取当前线程 ID"""
        try:
            from langgraph.config import get_config
            config = get_config()
            return config.get("configurable", {}).get("thread_id", "unknown")
        except Exception:
            return "unknown"

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage]],
    ) -> ToolMessage:
        """包装工具调用（异步版本）

        流程：
        1. 分析风险等级
        2. 记录审批请求
        3. 根据风险等级处理：
           - 黑名单：直接拒绝
           - 全自动模式：自动通过
           - 低危：直接执行
           - 高危/中危：触发审批
        """
        tool_name = request.tool_call.get("name", "unknown")
        args = request.tool_call.get("args", {})
        tool_call_id = request.tool_call.get("id", "")

        # 1. 分析风险等级
        risk_level = self.risk_analyzer.analyze(tool_name, args)

        # 2. 记录审批请求
        self.audit_logger.log_approval_request({
            "agent": self.current_agent,
            "tool": tool_name,
            "args": args,
            "risk_level": risk_level.value,
            "thread_id": self._get_thread_id(),
            "auto_mode": self.config.auto_mode_enabled,
        })

        # 3. 黑名单：直接拒绝
        if risk_level == RiskLevel.BLOCKED:
            self.audit_logger.log_blocked({
                "agent": self.current_agent,
                "tool": tool_name,
                "args": args,
            })
            return ToolMessage(
                content=f"❌ 操作被阻止：此命令在黑名单中，禁止执行。\n命令: {args.get('command', 'unknown')}",
                tool_call_id=tool_call_id,
            )

        # 4. 全自动模式：自动通过
        if self.config.should_auto_approve(risk_level):
            self.audit_logger.log_auto_approved({
                "agent": self.current_agent,
                "tool": tool_name,
                "args": args,
                "risk_level": risk_level.value,
            })
            result = await handler(request)
            self.audit_logger.log_execution_result({
                "request_id": tool_call_id,
                "success": True,
                "result": result.content if hasattr(result, "content") else str(result),
            })
            return result

        # 5. 低危：直接执行
        if risk_level == RiskLevel.LOW:
            result = await handler(request)
            self.audit_logger.log_execution_result({
                "request_id": tool_call_id,
                "success": True,
                "result": result.content if hasattr(result, "content") else str(result),
            })
            return result

        # 6. 高危/中危：触发审批
        allowed_decisions = self.config.get_allowed_decisions(tool_name)

        if not HAS_INTERRUPT:
            # 如果没有 interrupt 支持，记录警告并直接执行
            logging.warning(f"审批中间件缺少 interrupt 支持，自动通过高危操作: {tool_name}")
            self.audit_logger.log_auto_approved({
                "agent": self.current_agent,
                "tool": tool_name,
                "args": args,
                "risk_level": risk_level.value,
                "reason": "interrupt_not_available",
            })
            result = await handler(request)
            return result

        # 触发 interrupt
        approval = interrupt({
            "type": "tool_approval",
            "tool_name": tool_name,
            "args": args,
            "risk_level": risk_level.value,
            "allowed_decisions": allowed_decisions,
            "message": f"[{risk_level.value.upper()}风险] 请审批工具调用:\n工具: {tool_name}\n参数: {json.dumps(args, ensure_ascii=False)[:500]}",
        })

        # 处理审批决策
        decision = approval.get("type", "reject")

        self.audit_logger.log_approval_decision({
            "request_id": tool_call_id,
            "decision": decision,
            "approver": approval.get("approver", "user"),
            "edited_args": approval.get("edited_args") if decision == "edit" else None,
            "reason": approval.get("reason"),
        })

        if decision == "reject":
            return ToolMessage(
                content=f"❌ 用户拒绝了此操作。\n原因: {approval.get('reason', '未提供')}",
                tool_call_id=tool_call_id,
            )

        if decision == "edit":
            # 修改参数后执行
            edited_args = approval.get("edited_args", args)
            request.tool_call["args"] = edited_args

        # 执行工具
        result = await handler(request)

        self.audit_logger.log_execution_result({
            "request_id": tool_call_id,
            "success": True,
            "result": result.content if hasattr(result, "content") else str(result),
        })

        return result

    # 同步版本（调用异步版本）
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        """包装工具调用（同步版本）"""
        # 在同步上下文中运行异步代码
        return asyncio.run(self.awrap_tool_call(request, handler))


def create_approval_middleware(
    config_path: str = "config/approval_policy.yaml",
    current_agent: str = "unknown",
) -> HumanApprovalMiddleware:
    """创建审批中间件的便捷函数

    Args:
        config_path: YAML 配置文件路径
        current_agent: 当前 agent 名称

    Returns:
        HumanApprovalMiddleware: 审批中间件实例
    """
    config = ApprovalConfig.from_yaml(config_path)
    return HumanApprovalMiddleware(
        config=config,
        current_agent=current_agent,
    )