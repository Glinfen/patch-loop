"""User profile normalization."""


def normalize_email(email: str) -> str:
    """Normalize email casing and whitespace."""
    return email.strip().casefold()


def display_name(first: str, last: str) -> str:
    """Join profile names for display."""
    return f"{first.strip()} {last.strip()}".strip()
