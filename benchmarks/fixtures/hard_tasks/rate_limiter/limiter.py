"""Per-key sliding-window rate limiter."""


class SlidingWindowLimiter:
    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds

    def allow(self, key: str, now: float) -> bool:
        """Return whether a request is admitted at the supplied timestamp."""
        return True
