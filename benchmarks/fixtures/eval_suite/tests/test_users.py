from users import display_name, normalize_email


def test_user_helpers() -> None:
    assert normalize_email(" A@EXAMPLE.COM ") == "a@example.com"
    assert display_name("Ada", "Lovelace") == "Ada Lovelace"
