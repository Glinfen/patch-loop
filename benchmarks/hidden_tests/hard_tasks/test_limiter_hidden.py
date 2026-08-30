import pytest
from limiter import SlidingWindowLimiter


def test_keys_are_independent_and_denials_do_not_consume_capacity() -> None:
    limiter = SlidingWindowLimiter(max_requests=1, window_seconds=5.0)

    assert limiter.allow("alice", 1.0)
    assert not limiter.allow("alice", 2.0)
    assert limiter.allow("bob", 2.0)
    assert limiter.allow("alice", 6.0)


def test_configuration_and_per_key_time_must_be_valid() -> None:
    with pytest.raises(ValueError):
        SlidingWindowLimiter(max_requests=0, window_seconds=1.0)
    with pytest.raises(ValueError):
        SlidingWindowLimiter(max_requests=1, window_seconds=0.0)

    limiter = SlidingWindowLimiter(max_requests=2, window_seconds=3.0)
    assert limiter.allow("alice", 5.0)
    with pytest.raises(ValueError, match="nondecreasing"):
        limiter.allow("alice", 4.0)
