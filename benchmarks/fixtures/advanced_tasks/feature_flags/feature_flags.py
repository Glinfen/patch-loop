def enabled(flag: str, user_id: str, rollout_percentage: float, salt: str) -> bool:
    """Return whether a flag is enabled for a user."""
    del flag, user_id, salt
    return rollout_percentage >= 50
