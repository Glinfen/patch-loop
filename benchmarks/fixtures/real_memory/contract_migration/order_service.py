"""Order status response construction."""

from __future__ import annotations


def build_order_status(order_id: str, attempt: int, cache_hit: bool) -> dict[str, object]:
    """Build the public order status payload."""

    return {
        "status": "ok",
        "id": order_id,
        "retries": attempt,
    }
