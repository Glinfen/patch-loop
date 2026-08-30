import pytest
from migrations import migrate_document


def test_migrates_version_two_and_preserves_profile_fields() -> None:
    source = {
        "version": 2,
        "profile": {"display_name": "Grace", "timezone": "UTC"},
        "tags": " compiler, , navy ",
        "enabled": False,
    }

    assert migrate_document(source) == {
        "version": 3,
        "profile": {"display_name": "Grace", "timezone": "UTC"},
        "tags": ["compiler", "navy"],
        "enabled": False,
    }


def test_current_document_is_returned_as_a_deeply_detached_copy() -> None:
    source = {"version": 3, "profile": {"display_name": "Lin"}, "tags": ["ops"]}
    migrated = migrate_document(source)

    migrated["profile"]["display_name"] = "Changed"
    migrated["tags"].append("new")

    assert source == {"version": 3, "profile": {"display_name": "Lin"}, "tags": ["ops"]}


@pytest.mark.parametrize("document", [{}, {"version": 0}, {"version": 4}])
def test_rejects_missing_or_unsupported_versions(document: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"(?i)version"):
        migrate_document(document)
