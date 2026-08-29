"""CSV export helper."""


def export_row(values: list[str]) -> str:
    """Serialize one row using comma-separated values."""
    return ",".join(values)
