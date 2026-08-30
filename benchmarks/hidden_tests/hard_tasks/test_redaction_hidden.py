from redaction import redact_log


def test_redaction_is_case_insensitive_and_preserves_delimiters() -> None:
    message = "PASSWORD=hunter2, token=abc&next=1 Secret=xyz"

    assert redact_log(message) == ("PASSWORD=[REDACTED], token=[REDACTED]&next=1 Secret=[REDACTED]")


def test_multiple_bearer_tokens_and_benign_text() -> None:
    assert redact_log("healthy request") == "healthy request"
    assert redact_log("bearer first Bearer second") == ("bearer [REDACTED] Bearer [REDACTED]")
