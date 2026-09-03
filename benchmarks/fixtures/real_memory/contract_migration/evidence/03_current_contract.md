# Current order status contract — authoritative

Architecture decision ADR-42 supersedes the legacy contract in evidence 02.

`build_order_status` must return exactly three keys: `state`, `order_id`, and `retry`.

- `state` is always the exact string `ready`.
- `order_id` preserves the supplied non-empty identifier.
- `retry` is `0` for a cache hit; otherwise it is `max(0, attempt - 1)`.
- A blank identifier, including whitespace-only input, raises `ValueError` with the exact message `order_id is required`.
- A negative attempt raises `ValueError` with the exact message `attempt must be non-negative`.

No legacy aliases may appear in the returned mapping.
