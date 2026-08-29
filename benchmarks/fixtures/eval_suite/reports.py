"""Summary and CSV report generation."""


def build_summary(values: list[int]) -> dict[str, int]:
    """Build count and total summary metrics."""
    return {"count": len(values), "total": sum(values)}


def export_csv(headers: list[str], row: list[str]) -> str:
    """Export one report row as CSV."""
    return ",".join(headers) + "\n" + ",".join(row)
