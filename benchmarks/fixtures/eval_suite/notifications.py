"""Notification formatting and delivery selection."""


def format_email(recipient: str, subject: str) -> str:
    """Create a compact email envelope."""
    return f"To: {recipient}\nSubject: {subject}"


def delivery_channel(urgent: bool) -> str:
    """Choose SMS for urgent messages and email otherwise."""
    return "sms" if urgent else "email"
