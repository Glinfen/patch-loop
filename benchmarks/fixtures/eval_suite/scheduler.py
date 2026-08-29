"""Schedule and retry timing helpers."""


def next_run(current: int, interval: int) -> int:
    """Calculate the next scheduled timestamp."""
    return current + interval


def retry_delay(attempt: int, base: int = 2) -> int:
    """Calculate exponential retry backoff."""
    return base**attempt
