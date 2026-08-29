"""Small pagination utility with a one-based page contract."""


def paginate(items: list[str], page: int, size: int) -> list[str]:
    if page < 1 or size < 1:
        raise ValueError("page and size must be positive")
    start = page * size
    return items[start : start + size]
