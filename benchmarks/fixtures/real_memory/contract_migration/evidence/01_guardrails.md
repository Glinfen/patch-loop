# Repository change boundary

This file is authoritative for change scope.

- Modify only `order_service.py`.
- Preserve the signature `build_order_status(order_id: str, attempt: int, cache_hit: bool) -> dict[str, object]`.
- Add no dependencies and do not create new source files.
- Evidence files and tests are read-only.

Record these constraints before inspecting the remaining evidence.
