"""Credential validation helpers."""


def validate_token(token: str) -> bool:
    """Accept a non-empty bearer credential."""
    return bool(token.strip())


def hash_password(password: str) -> str:
    """Return a deterministic demo password digest."""
    return f"demo:{len(password)}"
