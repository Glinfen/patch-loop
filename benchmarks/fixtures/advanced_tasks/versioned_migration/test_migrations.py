from migrations import migrate_document


def test_migrates_a_version_one_document_without_mutating_it() -> None:
    source = {
        "version": 1,
        "name": "Ada",
        "tags": "ai, systems",
        "metadata": {"active": True},
    }

    migrated = migrate_document(source)

    assert migrated == {
        "version": 3,
        "profile": {"display_name": "Ada"},
        "tags": ["ai", "systems"],
        "metadata": {"active": True},
    }
    assert source["name"] == "Ada"
    assert source["tags"] == "ai, systems"
