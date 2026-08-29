"""Cache key and expiry helpers."""


def cache_key(namespace: str, identifier: str) -> str:
    """Build a namespaced cache key."""
    return f"{namespace}:{identifier}"


def ttl_expired(created_at: float, ttl: float, now: float) -> bool:
    """Check whether a cache entry exceeded its TTL."""
    return now >= created_at + ttl
