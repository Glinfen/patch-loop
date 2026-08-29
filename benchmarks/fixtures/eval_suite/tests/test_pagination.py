from pagination import next_cursor, paginate


def test_pagination_helpers() -> None:
    assert paginate(["a", "b", "c"], 2, 2) == ["c"]
    assert next_cursor(0, 2, 3) == 2
