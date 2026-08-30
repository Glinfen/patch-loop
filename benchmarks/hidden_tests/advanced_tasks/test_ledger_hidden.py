import pytest
from ledger import transfer
from models import Account, TransferReceipt


def make_accounts() -> dict[str, Account]:
    return {"a": Account("a", 50), "b": Account("b", 10)}


@pytest.mark.parametrize(
    ("source", "target", "amount", "message"),
    [
        ("missing", "b", 1, "unknown"),
        ("a", "missing", 1, "unknown"),
        ("a", "a", 1, "different"),
        ("a", "b", 0, "positive"),
        ("a", "b", -1, "positive"),
        ("a", "b", 51, "insufficient"),
    ],
)
def test_failures_leave_every_balance_unchanged(
    source: str, target: str, amount: int, message: str
) -> None:
    accounts = make_accounts()

    with pytest.raises(ValueError, match=f"(?i){message}"):
        transfer(accounts, source, target, amount)

    assert accounts == {"a": Account("a", 50), "b": Account("b", 10)}


def test_receipt_is_immutable_and_compares_by_value() -> None:
    receipt = transfer(make_accounts(), "a", "b", 10)
    assert receipt == TransferReceipt("a", "b", 10, 40, 20)

    with pytest.raises((AttributeError, TypeError)):
        receipt.amount = 99
