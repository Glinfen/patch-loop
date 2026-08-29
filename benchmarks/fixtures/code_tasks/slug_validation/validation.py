"""Public identifier validation."""


def valid_slug(slug: str) -> bool:
    """Accept non-empty alphanumeric slugs separated by single hyphens."""
    pieces = slug.split("-")
    return bool(slug) and all(piece.isalnum() for piece in pieces)
