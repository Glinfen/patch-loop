"""Configuration merge helpers."""


def deep_merge(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    """Merge override values into a configuration mapping."""
    result = dict(base)
    result.update(override)
    return result
