from cache import cache_key, ttl_expired


def test_cache_helpers() -> None:
    assert cache_key("user", "7") == "user:7"
    assert ttl_expired(10, 5, 15)
