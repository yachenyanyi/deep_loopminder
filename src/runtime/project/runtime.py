"""Runtime binding for Project persistence through LangGraph's official Store."""

from langgraph.config import get_store

from src.runtime.project.persistence import ProjectStore


def get_project_store() -> ProjectStore:
    """Bind Project persistence to the Store supplied by the active LangGraph runtime."""
    return ProjectStore(get_store())
