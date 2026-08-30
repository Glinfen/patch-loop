from dependency_order import dependency_order


def test_dependencies_come_before_dependents() -> None:
    graph = {
        "deploy": {"build"},
        "build": {"lint", "test"},
        "lint": set(),
        "test": set(),
    }

    order = dependency_order(graph)

    assert order.index("lint") < order.index("build")
    assert order.index("test") < order.index("build")
    assert order.index("build") < order.index("deploy")
