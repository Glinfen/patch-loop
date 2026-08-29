"""Invoice and discount calculations."""


def invoice_total(subtotal: float, tax_rate: float) -> float:
    """Calculate the tax-inclusive invoice amount."""
    return subtotal * (1 + tax_rate)


def apply_discount(amount: float, percent: float) -> float:
    """Apply a percentage discount to an amount."""
    return amount * (1 - percent / 100)
