import hashlib

from feature_flags import enabled


def reference_enabled(flag: str, user_id: str, percentage: float, salt: str) -> bool:
    payload = f"{salt}:{flag}:{user_id}".encode()
    bucket = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") % 10_000
    return bucket < int(percentage * 100)


def test_uses_the_documented_sha256_bucket() -> None:
    cases = [
        ("search-v2", "user-17", 12.5, "prod"),
        ("search-v2", "user-42", 12.5, "prod"),
        ("checkout", "user-17", 71.25, "staging"),
    ]

    for flag, user_id, percentage, salt in cases:
        assert enabled(flag, user_id, percentage, salt) is reference_enabled(
            flag, user_id, percentage, salt
        )
