from ledger import transfer
from models import Account, TransferReceipt


def test_transfers_funds_and_returns_the_resulting_balances() -> None:
    accounts = {
        "checking": Account("checking", 120),
        "savings": Account("savings", 30),
    }

    result = transfer(accounts, "checking", "savings", 45)

    assert result == TransferReceipt("checking", "savings", 45, 75, 75)
    assert accounts["checking"].balance == 75
    assert accounts["savings"].balance == 75
