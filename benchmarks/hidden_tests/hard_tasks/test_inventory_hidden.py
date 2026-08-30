import pytest
from inventory import reserve_many
from models import ReservationResult, StockItem


def test_duplicate_skus_are_aggregated() -> None:
    stock = {"A": StockItem("A", 5)}

    result = reserve_many(stock, [("A", 2), ("A", 1)])

    assert result == ReservationResult(reserved={"A": 3}, total_count=3)
    assert stock["A"].available == 2


def test_failure_is_atomic_and_invalid_quantities_are_rejected() -> None:
    stock = {"A": StockItem("A", 5), "B": StockItem("B", 1)}

    with pytest.raises(ValueError, match="insufficient"):
        reserve_many(stock, [("A", 2), ("B", 2)])
    assert stock["A"].available == 5
    assert stock["B"].available == 1

    with pytest.raises(ValueError, match="positive"):
        reserve_many(stock, [("A", 0)])
    with pytest.raises(ValueError, match="unknown"):
        reserve_many(stock, [("missing", 1)])
