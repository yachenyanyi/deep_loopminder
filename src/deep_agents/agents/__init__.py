"""Public agent exports, loaded only when requested."""

from importlib import import_module

_EXPORT_MODULES = {
    "create_intelligent_deep_agent_web": ".intelligent_web",
    "create_intelligent_deep_agent": ".intelligent_local",
    "create_role_playing_agent": ".role_playing",
    "create_basic_filesystem_agent": ".basic_filesystem",
    "create_teacher_agent": ".teacher_agent",
    "create_chat_agent": ".collaborative_agents",
    "create_coordinator_agent": ".collaborative_agents",
    "create_coder_agent": ".collaborative_agents",
    "create_researcher_agent": ".collaborative_agents",
    "create_assistant_agent": ".collaborative_agents",
    "COLLABORATIVE_EMPLOYEES": ".employee_registry",
    "get_employee_by_name": ".employee_registry",
    "get_all_employee_names": ".employee_registry",
}

__all__ = list(_EXPORT_MODULES)


def __getattr__(name):
    """Resolve existing public names without initializing unrelated agents."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    return getattr(import_module(module_name, __name__), name)
