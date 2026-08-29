from notifications import delivery_channel, format_email


def test_notification_helpers() -> None:
    assert "Subject: Hello" in format_email("a@example.com", "Hello")
    assert delivery_channel(True) == "sms"
