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

TASK_OUTCOME_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "active": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

TASK_RUNTIME_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "idle": frozenset({"running", "paused", "ended"}),
    "running": frozenset(
        {
            "pausing",
            "waiting_for_approval",
            "recovery_required",
            "ended",
        }
    ),
    "pausing": frozenset({"paused", "recovery_required"}),
    "paused": frozenset({"running", "ended"}),
    "waiting_for_approval": frozenset({"running", "paused", "recovery_required", "ended"}),
    # Leaving recovery_required is intentionally absent here. It must go
    # through one of the explicit recovery dispositions below.
    "recovery_required": frozenset(),
    "ended": frozenset(),
}

RECOVERY_DISPOSITION_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "confirm_result": frozenset({"running", "paused", "ended"}),
    "create_retry": frozenset({"waiting_for_approval"}),
    "abandon": frozenset({"ended"}),
}

CONTROL_REQUEST_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "requested": frozenset({"acknowledged", "cleanup_failed"}),
    "acknowledged": frozenset({"settled", "cleanup_failed"}),
    "settled": frozenset(),
    "cleanup_failed": frozenset(),
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


def recovery_transition_is_allowed(disposition: str, target: str) -> bool:
    """Return whether an explicit recovery disposition permits a target state."""

    return target in RECOVERY_DISPOSITION_TRANSITIONS.get(disposition, frozenset())
