"""ApprovalRequest - 审批请求数据类

包含工具调用的上下文信息，传递给 ApprovalProvider 进行评估。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ApprovalRequest:
    """审批请求

    包含工具调用的完整上下文，用于 Provider 评估。

    Attributes:
        tool_name: 工具名称，如 "shell", "write_file"
        tool_input: 工具参数，如 {"command": "rm test.txt"}
        agent_id: 调用工具的代理ID
        thread_id: LangGraph 线程ID
        timestamp: 请求时间戳
        context: 额外上下文信息
    """

    tool_name: str
    tool_input: dict[str, Any]
    agent_id: str | None = None
    thread_id: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """确保 tool_input 是字典"""
        if not isinstance(self.tool_input, dict):
            self.tool_input = {}