from inventory import reserve_many
from models import ReservationResult, StockItem


def test_successful_reservation_updates_stock_and_returns_result() -> None:
    stock = {
        "A": StockItem("A", 5),
        "B": StockItem("B", 3),
    }

    result = reserve_many(stock, [("A", 2), ("B", 1)])

    assert result == ReservationResult(reserved={"A": 2, "B": 1}, total_count=3)
    assert stock["A"].available == 3
    assert stock["B"].available == 2
