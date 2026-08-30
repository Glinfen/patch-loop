import hashlib

import pytest
from feature_flags import enabled


def reference_enabled(flag: str, user_id: str, percentage: float, salt: str) -> bool:
    payload = f"{salt}:{flag}:{user_id}".encode()
    bucket = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") % 10_000
    return bucket < int(percentage * 100)


@pytest.mark.parametrize(
    ("flag", "user_id", "percentage", "salt"),
    [
        ("推荐系统", "用户-甲", 0.01, "生产"),
        ("推荐系统", "用户-乙", 99.99, "生产"),
        ("billing", "customer:100", 33.33, "2026-08"),
    ],
)
def test_matches_reference_for_unicode_and_fractional_rollouts(
    flag: str, user_id: str, percentage: float, salt: str
) -> None:
    expected = reference_enabled(flag, user_id, percentage, salt)
    assert enabled(flag, user_id, percentage, salt) is expected
    assert enabled(flag, user_id, percentage, salt) is expected


def test_zero_and_one_hundred_are_exact_boundaries() -> None:
    assert enabled("flag", "user", 0, "salt") is False
    assert enabled("flag", "user", 100, "salt") is True


@pytest.mark.parametrize("percentage", [-0.01, 100.01])
def test_rejects_percentages_outside_the_closed_interval(percentage: float) -> None:
    with pytest.raises(ValueError, match=r"(?i)percentage"):
        enabled("flag", "user", percentage, "salt")
