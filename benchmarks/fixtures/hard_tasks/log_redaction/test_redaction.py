from redaction import redact_log


def test_key_values_and_bearer_tokens_are_redacted() -> None:
    assert redact_log("api_key=abc123 status=ok") == "api_key=[REDACTED] status=ok"
    assert redact_log("Authorization: Bearer token-value") == "Authorization: Bearer [REDACTED]"
