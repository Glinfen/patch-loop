from billing import apply_discount, invoice_total


def test_billing_helpers() -> None:
    assert invoice_total(100, 0.2) == 120
    assert apply_discount(100, 25) == 75
