"""Retry timing policy."""


def retry_delay(attempt: int, base_seconds: int = 2) -> int:
    """Return exponential delay for a zero-based retry attempt."""
    if attempt < 0:
        raise ValueError("attempt cannot be negative")
    return base_seconds**attempt
