# Legacy order status contract — obsolete

Historical clients once expected keys `status`, `id`, and `retries`. The success value was `ok`, and the attempt number was copied directly into `retries`.

This contract is retained only for migration history. A later authoritative decision supersedes every field described here. Do not implement this version.
