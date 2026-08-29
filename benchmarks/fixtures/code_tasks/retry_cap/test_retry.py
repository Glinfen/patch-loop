import pytest
from retry import retry_delay


def test_retry_delay_supports_a_maximum() -> None:
    assert retry_delay(0, maximum_seconds=10) == 1
    assert retry_delay(3, maximum_seconds=10) == 8
    assert retry_delay(4, maximum_seconds=10) == 10


def test_negative_attempt_is_rejected() -> None:
    with pytest.raises(ValueError):
        retry_delay(-1, maximum_seconds=10)
