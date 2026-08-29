import pytest
from pagination import paginate


def test_pages_are_one_based() -> None:
    items = ["a", "b", "c", "d", "e"]
    assert paginate(items, 1, 2) == ["a", "b"]
    assert paginate(items, 2, 2) == ["c", "d"]
    assert paginate(items, 3, 2) == ["e"]


def test_invalid_page_is_rejected() -> None:
    with pytest.raises(ValueError):
        paginate(["a"], 0, 1)
