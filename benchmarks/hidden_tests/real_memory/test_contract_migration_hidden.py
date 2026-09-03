from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest


def _load_service() -> ModuleType:
    repository = Path(os.environ["PATCHLOOP_REAL_MEMORY_REPO"])
    path = repository / "order_service.py"
    spec = importlib.util.spec_from_file_location("real_memory_order_service", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_contract_and_superseded_fields() -> None:
    service = _load_service()

    assert service.build_order_status("order-7", 4, False) == {
        "state": "ready",
        "order_id": "order-7",
        "retry": 3,
    }
    assert service.build_order_status("order-8", 9, True) == {
        "state": "ready",
        "order_id": "order-8",
        "retry": 0,
    }
    assert service.build_order_status("order-9", 0, False)["retry"] == 0


@pytest.mark.parametrize("order_id", ["", " ", "\t\n"])
def test_blank_order_id_is_rejected(order_id: str) -> None:
    service = _load_service()

    with pytest.raises(ValueError, match=r"^order_id is required$"):
        service.build_order_status(order_id, 1, False)


def test_negative_attempt_is_rejected() -> None:
    service = _load_service()

    with pytest.raises(ValueError, match=r"^attempt must be non-negative$"):
        service.build_order_status("order-10", -1, False)
