import pytest
from pydantic import ValidationError

from patchloop.domain import (
    Task,
    TaskOutcome,
    TaskRuntimeCondition,
    TaskStatus,
)
from patchloop.session.models import Session, SessionCheckpoint, Turn, TurnRole


def test_legacy_task_status_is_projected_to_new_state_dimensions(tmp_path) -> None:
    task = Task.model_validate(
        {
            "id": "legacy-task",
            "goal": "Inspect",
            "repository": str(tmp_path),
            "status": "completed",
        }
    )

    assert task.outcome is TaskOutcome.COMPLETED
    assert task.runtime_condition is TaskRuntimeCondition.ENDED
    assert task.status is TaskStatus.COMPLETED
    assert task.version == 1


def test_task_runtime_condition_has_explicit_recovery_boundary(tmp_path) -> None:
    task = Task(id="session-task", goal="Inspect", repository=str(tmp_path))
    task.transition(TaskStatus.RUNNING)
    task.transition_runtime(TaskRuntimeCondition.WAITING_FOR_APPROVAL)
    task.transition_runtime(TaskRuntimeCondition.RUNNING)
    task.transition_runtime(TaskRuntimeCondition.RECOVERY_REQUIRED)

    with pytest.raises(ValueError, match="invalid task runtime transition"):
        task.transition_runtime(TaskRuntimeCondition.RUNNING)


def test_terminal_task_cannot_be_constructed_as_active(tmp_path) -> None:
    with pytest.raises(ValidationError, match="compatibility projection"):
        Task(
            id="bad-task",
            goal="Invalid",
            repository=str(tmp_path),
            status=TaskStatus.COMPLETED,
            outcome=TaskOutcome.ACTIVE,
        )


def test_session_close_requires_no_active_task() -> None:
    session = Session(id="session-1", workspace_ref="workspace")
    session.close()
    assert session.version == 2
    assert session.status.value == "closed"
    session.close()

    active = Session(id="session-2", workspace_ref="workspace", active_task_id="task-1")
    with pytest.raises(ValueError, match="active task"):
        active.close()


def test_turn_and_checkpoint_are_round_trip_serializable(tmp_path) -> None:
    turn = Turn(
        id="turn-1",
        session_id="session-1",
        role=TurnRole.USER,
        content="Please inspect the repository.",
        sequence=1,
        client_submission_id="client-1",
    )
    checkpoint = SessionCheckpoint(
        session_id="session-1",
        task_id="task-1",
        turn_id=turn.id,
        consumed_input_sequence=turn.sequence,
        event_sequence=2,
        pending_effect_ids=["effect-1"],
    )

    restored = SessionCheckpoint.model_validate_json(checkpoint.model_dump_json())
    assert restored == checkpoint
    assert turn.session_id == "session-1"
