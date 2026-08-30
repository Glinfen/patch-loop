from dataclasses import dataclass


@dataclass
class Account:
    account_id: str
    balance: int
