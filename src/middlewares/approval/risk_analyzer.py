"""RiskAnalyzer - 风险分析器

分析工具调用的风险等级，用于 YamlPolicyProvider。
"""

from src.middlewares.approval.decision import RiskLevel


class ApprovalConfig:
    """审批配置（简化版本，用于 RiskAnalyzer）

    从 YAML 配置文件加载，或使用默认值。
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
        auto_mode_enabled: bool = False,
    ):
        self.blacklist = blacklist or self.DEFAULT_BLACKLIST
        self.high_risk = high_risk or self.DEFAULT_HIGH_RISK
        self.medium_risk = medium_risk or self.DEFAULT_MEDIUM_RISK
        self.whitelist = whitelist or self.DEFAULT_WHITELIST
        self.tool_config = tool_config or {}
        self.auto_mode_enabled = auto_mode_enabled

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
        if tool_name in ["run_shell_command", "shell"]:
            return self._analyze_shell_command(args.get("command", ""))
        elif tool_name == "call_tool":
            return self._analyze_mcp_tool(args.get("tool_name", ""), args.get("args", {}))
        elif tool_name == "write_file":
            return self._analyze_write_file(args.get("file_path", ""), args.get("content", ""))
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
        # MCP 工具默认为中危
        return RiskLevel.MEDIUM

    def _analyze_write_file(self, file_path: str, content: str) -> RiskLevel:
        """分析写入文件的风险"""
        path_lower = file_path.lower()

        # 高危路径：敏感配置文件
        high_risk_paths = [
            ".env",
            "config/",
            "secrets/",
            "credentials",
            "password",
            "private_key",
            "id_rsa",
            ".pem",
            ".key",
        ]
        for pattern in high_risk_paths:
            if pattern in path_lower:
                return RiskLevel.HIGH

        # 中危路径：重要文件
        medium_risk_paths = [
            "README",
            "LICENSE",
            "package.json",
            "requirements.txt",
            "pyproject.toml",
            ".git/",
        ]
        for pattern in medium_risk_paths:
            if pattern in path_lower:
                return RiskLevel.MEDIUM

        # 默认：低危
        return RiskLevel.LOW

    def _analyze_generic_tool(self, tool_name: str) -> RiskLevel:
        """分析通用工具的风险"""
        # 协作类工具不需要审批
        if tool_name in ["collaborate", "check_colleague", "get_thread_info", "list_resources"]:
            return RiskLevel.LOW

        # 其他工具默认低危
        return RiskLevel.LOW