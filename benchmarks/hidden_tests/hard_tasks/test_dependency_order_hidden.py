import pytest
from dependency_order import dependency_order


def test_ready_nodes_are_selected_lexicographically() -> None:
    graph = {"app": {"db", "cache"}, "db": set(), "cache": set()}

    assert dependency_order(graph) == ["cache", "db", "app"]


def test_cycle_and_unknown_dependency_are_rejected() -> None:
    with pytest.raises(ValueError, match="cycle"):
        dependency_order({"a": {"b"}, "b": {"a"}})
    with pytest.raises(ValueError, match="unknown"):
        dependency_order({"app": {"missing"}})
