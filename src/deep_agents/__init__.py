"""Public Deep Agents exports, loaded only when requested."""

from importlib import import_module

_DB_EXPORTS = (
    "init_postgres_checkpointer",
    "init_postgres_store",
    "get_postgres_store",
    "cleanup_postgres",
    "global_checkpointer",
    "global_store",
)
_CONFIG_EXPORTS = (
    "BASE_DIR",
    "WORKSPACE_DIR",
    "SKILLS_REPO_DIR",
    "load_prompt_from_file",
)
_AGENT_EXPORTS = (
    "create_intelligent_deep_agent_web",
    "create_intelligent_deep_agent",
    "create_role_playing_agent",
    "create_basic_filesystem_agent",
    "create_teacher_agent",
    "create_chat_agent",
    "create_coordinator_agent",
    "create_coder_agent",
    "create_researcher_agent",
    "create_assistant_agent",
    "COLLABORATIVE_EMPLOYEES",
    "get_employee_by_name",
    "get_all_employee_names",
)

__all__ = [*_DB_EXPORTS, *_CONFIG_EXPORTS, *_AGENT_EXPORTS]


def __getattr__(name):
    """Preserve public imports while keeping independent agents isolated."""
    if name in _DB_EXPORTS:
        module_name = ".db"
    elif name in _CONFIG_EXPORTS:
        module_name = ".config"
    elif name in _AGENT_EXPORTS:
        module_name = ".agents"
    else:
        raise AttributeError(name)
    return getattr(import_module(module_name, __name__), name)
