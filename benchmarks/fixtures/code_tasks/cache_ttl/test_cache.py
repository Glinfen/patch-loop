from cache import ttl_expired


def test_ttl_boundary_is_expired() -> None:
    assert not ttl_expired(100.0, 30.0, 129.999)
    assert ttl_expired(100.0, 30.0, 130.0)
    assert ttl_expired(100.0, 30.0, 131.0)
