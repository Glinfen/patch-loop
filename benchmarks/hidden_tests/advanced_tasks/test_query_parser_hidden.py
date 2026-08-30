import pytest
from query_parser import parse_query


def test_decodes_every_supported_escape() -> None:
    assert parse_query(r"formula=a\=b,path=c:\\tmp,note=x\,y") == {
        "formula": "a=b",
        "path": r"c:\tmp",
        "note": "x,y",
    }


def test_empty_input_represents_no_assignments() -> None:
    assert parse_query("") == {}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("novalue", "assignment"),
        ("=value", "key"),
        ("a=1,a=2", "duplicate"),
        ("a=trailing\\", "escape"),
        (r"a=bad\q", "escape"),
    ],
)
def test_rejects_malformed_inputs(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=f"(?i){message}"):
        parse_query(text)
