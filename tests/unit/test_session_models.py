import pytest
from pydantic import ValidationError

from patchloop.domain import (
    Task,
    TaskOutcome,
    TaskRuntimeCondition,
    TaskStatus,
)
from patchloop.persistence import RuntimeCheckpoint
from patchloop.prompt_cache import AppendOnlyPromptState
from patchloop.providers import ModelMessage
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


def test_session_checkpoint_round_trips_append_only_state(tmp_path) -> None:
    state = AppendOnlyPromptState(
        root_prefix_message_count=2,
        last_submitted_message_count=2,
        last_submitted_message_fingerprints=["a" * 64, "b" * 64],
        last_submitted_request_id="request-1",
    )
    messages = [
        ModelMessage(role="system", content="static"),
        ModelMessage(role="user", content="inspect"),
        ModelMessage(role="assistant", content="done"),
    ]
    checkpoint = SessionCheckpoint(
        session_id="session-1",
        task_id="task-1",
        messages=messages,
        append_only_state=state,
    )

    restored = SessionCheckpoint.model_validate_json(checkpoint.model_dump_json())
    assert restored.append_only_state == state
    runtime = RuntimeCheckpoint(
        task_id="task-1",
        next_step_index=1,
        messages=messages,
        append_only_state=state,
    )
    assert runtime.append_only_state == restored.append_only_state
    without_state = checkpoint.model_dump(mode="json")
    without_state.pop("append_only_state")
    assert SessionCheckpoint.model_validate(without_state).append_only_state is None


def test_session_checkpoint_rejects_state_beyond_its_transcript(tmp_path) -> None:
    beyond_submission = AppendOnlyPromptState(
        root_prefix_message_count=2,
        last_submitted_message_count=4,
        last_submitted_message_fingerprints=["a" * 64, "b" * 64, "c" * 64, "d" * 64],
    )
    with pytest.raises(ValidationError, match="last submitted request"):
        SessionCheckpoint(
            session_id="session-1",
            task_id="task-1",
            messages=[
                ModelMessage(role="system", content="static"),
                ModelMessage(role="user", content="inspect"),
                ModelMessage(role="assistant", content="done"),
            ],
            append_only_state=beyond_submission,
        )

    oversized_root = AppendOnlyPromptState(root_prefix_message_count=4)
    with pytest.raises(ValidationError, match="declared root prefix"):
        SessionCheckpoint(
            session_id="session-1",
            task_id="task-1",
            append_only_state=oversized_root,
        )
