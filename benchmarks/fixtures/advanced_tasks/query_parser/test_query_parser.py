from query_parser import parse_query


def test_parses_basic_assignments_and_escaped_commas() -> None:
    assert parse_query(r"name=ada\,lovelace,role=engineer") == {
        "name": "ada,lovelace",
        "role": "engineer",
    }
