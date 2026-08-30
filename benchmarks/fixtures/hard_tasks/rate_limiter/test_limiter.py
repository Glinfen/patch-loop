from limiter import SlidingWindowLimiter


def test_capacity_and_exact_window_boundary() -> None:
    limiter = SlidingWindowLimiter(max_requests=2, window_seconds=10.0)

    assert limiter.allow("alice", 0.0)
    assert limiter.allow("alice", 1.0)
    assert not limiter.allow("alice", 2.0)
    assert limiter.allow("alice", 10.0)
