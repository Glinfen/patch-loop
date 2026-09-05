"""SRF-00 contract data kept independent from the production domain models.

SRF-01 will turn these strings into serializable domain enums. Keeping the
matrix here first makes the intended transitions executable before the new
models exist and prevents each later layer from inventing its own rules.
"""

from __future__ import annotations

from collections.abc import Mapping

SESSION_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "open": frozenset({"closed"}),
    "closed": frozenset(),
}

TASK_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "created": frozenset({"running", "paused", "waiting_for_approval", "cancelled"}),
    "running": frozenset(
        {
            "paused",
            "waiting_for_approval",
            "completed",
            "failed",
            "cancelled",
            "recovery_required",
        }
    ),
    "paused": frozenset({"running", "cancelled"}),
    "waiting_for_approval": frozenset({"running", "paused", "cancelled"}),
    "recovery_required": frozenset({"cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

EFFECT_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "prepared": frozenset({"waiting_for_approval", "executing", "denied"}),
    "waiting_for_approval": frozenset({"prepared", "denied", "cancelled"}),
    "executing": frozenset({"succeeded", "failed", "unknown"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "denied": frozenset(),
    "unknown": frozenset(),
    "cancelled": frozenset(),
}


def transition_is_allowed(
    transitions: Mapping[str, frozenset[str]], source: str, target: str
) -> bool:
    """Return whether a contract transition is explicitly allowed."""

    return target in transitions.get(source, frozenset())
