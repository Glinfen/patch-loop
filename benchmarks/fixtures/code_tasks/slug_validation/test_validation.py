from validation import valid_slug


def test_slug_requires_lowercase_nonempty_segments() -> None:
    assert valid_slug("alpha")
    assert valid_slug("alpha-42")
    assert not valid_slug("Alpha")
    assert not valid_slug("alpha--beta")
    assert not valid_slug("-alpha")
    assert not valid_slug("")
