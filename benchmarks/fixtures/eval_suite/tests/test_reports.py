from reports import build_summary, export_csv


def test_report_helpers() -> None:
    assert build_summary([1, 2]) == {"count": 2, "total": 3}
    assert export_csv(["name"], ["Ada"]) == "name\nAda"
