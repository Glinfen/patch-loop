from auth import hash_password, validate_token


def test_auth_helpers() -> None:
    assert validate_token(" token ")
    assert hash_password("abc") == "demo:3"
