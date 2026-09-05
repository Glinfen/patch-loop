import pytest

from tests.support.session_contract import (
    CONTROL_REQUEST_TRANSITIONS,
    EFFECT_TRANSITIONS,
    RECOVERY_DISPOSITION_TRANSITIONS,
    SESSION_TRANSITIONS,
    TASK_OUTCOME_TRANSITIONS,
    TASK_RUNTIME_TRANSITIONS,
    recovery_transition_is_allowed,
    transition_is_allowed,
)


@pytest.mark.parametrize(
    ("transitions", "source", "target"),
    [
        (SESSION_TRANSITIONS, "open", "closed"),
        (TASK_OUTCOME_TRANSITIONS, "active", "completed"),
        (TASK_OUTCOME_TRANSITIONS, "active", "cancelled"),
        (TASK_RUNTIME_TRANSITIONS, "idle", "running"),
        (TASK_RUNTIME_TRANSITIONS, "running", "pausing"),
        (TASK_RUNTIME_TRANSITIONS, "running", "waiting_for_approval"),
        (TASK_RUNTIME_TRANSITIONS, "pausing", "paused"),
        (TASK_RUNTIME_TRANSITIONS, "paused", "running"),
        (CONTROL_REQUEST_TRANSITIONS, "requested", "acknowledged"),
        (CONTROL_REQUEST_TRANSITIONS, "acknowledged", "settled"),
        (EFFECT_TRANSITIONS, "prepared", "executing"),
        (EFFECT_TRANSITIONS, "executing", "unknown"),
        (EFFECT_TRANSITIONS, "waiting_for_approval", "cancelled"),
    ],
)
def test_srf00_contract_allows_explicit_transitions(
    transitions: object, source: str, target: str
) -> None:
    assert transition_is_allowed(transitions, source, target)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("transitions", "source", "target"),
    [
        (SESSION_TRANSITIONS, "closed", "open"),
        (TASK_OUTCOME_TRANSITIONS, "completed", "active"),
        (TASK_OUTCOME_TRANSITIONS, "cancelled", "active"),
        (TASK_RUNTIME_TRANSITIONS, "recovery_required", "running"),
        (TASK_RUNTIME_TRANSITIONS, "running", "paused"),
        (CONTROL_REQUEST_TRANSITIONS, "cleanup_failed", "settled"),
        (EFFECT_TRANSITIONS, "unknown", "prepared"),
        (EFFECT_TRANSITIONS, "succeeded", "executing"),
        (EFFECT_TRANSITIONS, "denied", "executing"),
    ],
)
def test_srf00_contract_rejects_unsafe_transitions(
    transitions: object, source: str, target: str
) -> None:
    assert not transition_is_allowed(transitions, source, target)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("disposition", "target"),
    [
        ("confirm_result", "running"),
        ("confirm_result", "ended"),
        ("create_retry", "waiting_for_approval"),
        ("abandon", "ended"),
    ],
)
def test_srf00_recovery_requires_an_explicit_disposition(disposition: str, target: str) -> None:
    assert recovery_transition_is_allowed(disposition, target)
    assert target in RECOVERY_DISPOSITION_TRANSITIONS[disposition]


@pytest.mark.parametrize(
    ("disposition", "target"),
    [
        ("", "running"),
        ("create_retry", "running"),
        ("abandon", "running"),
    ],
)
def test_srf00_recovery_rejects_implicit_or_mismatched_dispositions(
    disposition: str, target: str
) -> None:
    assert not recovery_transition_is_allowed(disposition, target)
