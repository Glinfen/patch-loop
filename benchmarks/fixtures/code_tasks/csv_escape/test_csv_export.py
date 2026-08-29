from csv_export import export_row


def test_csv_special_characters_are_escaped() -> None:
    assert export_row(["alpha", "beta"]) == "alpha,beta"
    assert export_row(["last, first", "plain"]) == '"last, first",plain'
    assert export_row(['say "hello"', "plain"]) == '"say ""hello""",plain'
    assert export_row(["line\nbreak", "plain"]) == '"line\nbreak",plain'
