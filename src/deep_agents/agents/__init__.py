# 现有代理
from .intelligent_web import create_intelligent_deep_agent_web
from .intelligent_local import create_intelligent_deep_agent
from .role_playing import create_role_playing_agent
from .basic_filesystem import create_basic_filesystem_agent

# 协作智能体
from .collaborative_agents import (
    create_chat_agent,
    create_coordinator_agent,
    create_coder_agent,
    create_researcher_agent,
    create_assistant_agent,
)

# 员工注册表
from .employee_registry import COLLABORATIVE_EMPLOYEES, get_employee_by_name, get_all_employee_names

__all__ = [
    "create_intelligent_deep_agent_web",
    "create_intelligent_deep_agent",
    "create_role_playing_agent",
    "create_basic_filesystem_agent",
    # 协作智能体
    "create_chat_agent",
    "create_coordinator_agent",
    "create_coder_agent",
    "create_researcher_agent",
    "create_assistant_agent",
    # 员工注册表
    "COLLABORATIVE_EMPLOYEES",
    "get_employee_by_name",
    "get_all_employee_names",
]