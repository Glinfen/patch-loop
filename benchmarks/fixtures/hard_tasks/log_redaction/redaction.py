"""Sensitive value redaction for log messages."""


def redact_log(message: str) -> str:
    """Redact credentials from a log message."""
    if "password=" not in message:
        return message
    prefix, _ = message.split("password=", 1)
    return prefix + "password=[REDACTED]"
