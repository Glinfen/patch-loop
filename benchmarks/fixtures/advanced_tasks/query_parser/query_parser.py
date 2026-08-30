def parse_query(text: str) -> dict[str, str]:
    """Parse comma-separated key=value assignments."""
    return dict(part.split("=", 1) for part in text.split(","))
