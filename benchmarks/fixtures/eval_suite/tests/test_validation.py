from validation import validate_quantity, validate_slug


def test_validation_helpers() -> None:
    assert validate_slug("valid-slug")
    assert not validate_slug("Invalid")
    assert validate_quantity(1)
