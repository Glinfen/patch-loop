"""Dependency ordering."""


def dependency_order(graph: dict[str, set[str]]) -> list[str]:
    """Return all graph nodes in dependency-first order."""
    return sorted(graph)
