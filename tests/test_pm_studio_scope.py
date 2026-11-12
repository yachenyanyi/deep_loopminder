"""Verify Studio test chats cannot select or write a real project."""

from types import SimpleNamespace

import pytest

from src.middlewares.project_state.scope import resolve_project_id
from src.middlewares.project_state.tools import require_writer


def test_studio_scope_ignores_supplied_project_and_is_per_thread():
    config = {"configurable": {"graph_id": "pm_agent_studio", "thread_id": "one", "project_id": "real-project", "project_state_single_writer": True}}
    assert resolve_project_id(config) == "studio-thread:one"
    with pytest.raises(ValueError, match="disabled in Studio"):
        require_writer(SimpleNamespace(config=config))
    config["configurable"]["thread_id"] = "two"
    assert resolve_project_id(config) == "studio-thread:two"


def test_real_scope_requires_explicit_non_studio_project():
    config = {"configurable": {"graph_id": "pm_agent", "thread_id": "one"}}
    with pytest.raises(ValueError, match="project_id is required"):
        resolve_project_id(config)
    config["configurable"]["project_id"] = "studio-thread:one"
    with pytest.raises(ValueError, match="reserved"):
        resolve_project_id(config)
    config["configurable"]["project_id"] = "real-project"
    assert resolve_project_id(config) == "real-project"
