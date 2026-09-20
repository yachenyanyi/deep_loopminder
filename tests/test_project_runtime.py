from unittest.mock import patch

from langgraph.store.memory import InMemoryStore

from src.runtime.project.persistence import ProjectStore
from src.runtime.project.runtime import get_project_store


def test_project_store_uses_active_langgraph_runtime_store() -> None:
    store = InMemoryStore()

    with patch("src.runtime.project.runtime.get_store", return_value=store) as accessor:
        repository = get_project_store()

    accessor.assert_called_once_with()
    assert isinstance(repository, ProjectStore)
    assert repository._store is store


def test_project_store_does_not_fallback_when_runtime_store_is_unavailable() -> None:
    with patch(
        "src.runtime.project.runtime.get_store",
        side_effect=RuntimeError("no active runtime store"),
    ):
        try:
            get_project_store()
        except RuntimeError as exc:
            assert str(exc) == "no active runtime store"
        else:
            raise AssertionError("Project runtime binding must fail closed without official Store")
