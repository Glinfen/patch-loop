"""Stock reservation operations."""


def reserve_stock(available: int, requested: int) -> int:
    """Return remaining stock after a valid reservation."""
    if requested > available:
        raise ValueError("insufficient stock")
    return available - requested


def release_stock(available: int, released: int) -> int:
    """Return stock after releasing a reservation."""
    return available + released
