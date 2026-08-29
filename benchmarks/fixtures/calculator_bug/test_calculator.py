from calculator import divide


def test_divide_preserves_fractional_result() -> None:
    assert divide(5, 2) == 2.5
