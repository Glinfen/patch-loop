"""Page slicing and cursor helpers."""


def paginate(items: list[str], page: int, size: int) -> list[str]:
    """Return one one-based page of items."""
    start = (page - 1) * size
    return items[start : start + size]


def next_cursor(offset: int, size: int, total: int) -> int | None:
    """Return the next offset when more records exist."""
    candidate = offset + size
    return candidate if candidate < total else None
