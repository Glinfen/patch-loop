from order_service import build_order_status


def test_build_order_status_returns_a_new_mapping() -> None:
    first = build_order_status("order-1", 2, False)
    second = build_order_status("order-1", 2, False)

    assert isinstance(first, dict)
    assert first is not second
