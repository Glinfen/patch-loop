"""Inventory domain models."""

from dataclasses import dataclass


@dataclass
class StockItem:
    sku: str
    available: int
