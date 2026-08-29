import pytest
from inventory import release_stock, reserve_stock


def test_inventory_helpers() -> None:
    assert reserve_stock(5, 2) == 3
    assert release_stock(3, 2) == 5
    with pytest.raises(ValueError):
        reserve_stock(1, 2)
