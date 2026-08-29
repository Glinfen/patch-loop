"""Input validation rules."""


def validate_slug(slug: str) -> bool:
    """Accept lowercase hyphenated slugs."""
    return bool(slug) and slug.replace("-", "").isalnum() and slug == slug.casefold()


def validate_quantity(quantity: int) -> bool:
    """Accept positive order quantities."""
    return quantity > 0
