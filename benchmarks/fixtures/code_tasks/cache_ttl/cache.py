"""Cache expiry helpers."""


def ttl_expired(created_at: float, ttl_seconds: float, now: float) -> bool:
    """Return whether the entry has reached its expiration instant."""
    return now > created_at + ttl_seconds
