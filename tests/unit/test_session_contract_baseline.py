import pytest

from tests.support.session_contract import (
    EFFECT_TRANSITIONS,
    SESSION_TRANSITIONS,
    TASK_TRANSITIONS,
    transition_is_allowed,
)


@pytest.mark.parametrize(
    ("transitions", "source", "target"),
    [
        (SESSION_TRANSITIONS, "open", "closed"),
        (TASK_TRANSITIONS, "created", "running"),
        (TASK_TRANSITIONS, "running", "paused"),
        (TASK_TRANSITIONS, "running", "waiting_for_approval"),
        (TASK_TRANSITIONS, "paused", "running"),
        (TASK_TRANSITIONS, "recovery_required", "cancelled"),
        (EFFECT_TRANSITIONS, "prepared", "executing"),
        (EFFECT_TRANSITIONS, "executing", "unknown"),
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
        (TASK_TRANSITIONS, "completed", "running"),
        (TASK_TRANSITIONS, "cancelled", "running"),
        (TASK_TRANSITIONS, "recovery_required", "running"),
        (EFFECT_TRANSITIONS, "unknown", "prepared"),
        (EFFECT_TRANSITIONS, "succeeded", "executing"),
        (EFFECT_TRANSITIONS, "denied", "executing"),
    ],
)
def test_srf00_contract_rejects_unsafe_transitions(
    transitions: object, source: str, target: str
) -> None:
    assert not transition_is_allowed(transitions, source, target)  # type: ignore[arg-type]
