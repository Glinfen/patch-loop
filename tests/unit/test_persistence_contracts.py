from datetime import UTC, datetime, timedelta

import pytest

from patchloop.domain import Task, TaskRuntimeCondition
from patchloop.execution.models import (
    Effect,
    EffectStatus,
    Execution,
    RecoveryDisposition,
    RecoveryDispositionKind,
)
from patchloop.persistence_contracts import (
    EffectIdentityConflict,
    FakeStore,
    LeaseConflict,
    LeaseGuard,
    StaleVersion,
    SubmissionConflict,
)
from patchloop.session.models import Session, Turn, TurnRole


def _effect(effect_id: str = "effect-1", *, step_id: str = "step-1") -> Effect:
    return Effect(
        id=effect_id,
        task_id="task-1",
        step_id=step_id,
        batch_position=0,
        provider_call_id=f"provider-{effect_id}",
        tool_name="write_file",
        arguments_summary={"path": "app.py"},
    )


def _execution() -> Execution:
    return Execution(
        id="execution-1",
        session_id="session-1",
        task_id="task-1",
        owner_id="worker-1",
        lease_token="token-1",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )


def test_fake_store_deduplicates_client_turns_and_rejects_conflicts() -> None:
    store = FakeStore()
    session = store.create_session(Session(id="session-1", workspace_ref="workspace"))
    first = store.append_turn(
        Turn(
            session_id=session.id,
            role=TurnRole.USER,
            content="Inspect",
            client_submission_id="client-1",
        ),
        expected_version=session.version,
    )
    assert (
        store.append_turn(
            Turn(
                session_id=session.id,
                role=TurnRole.USER,
                content="Inspect",
                client_submission_id="client-1",
            ),
            expected_version=store.get_session(session.id).version,
        ).id
        == first.id
    )
    with pytest.raises(SubmissionConflict):
        store.append_turn(
            Turn(
                session_id=session.id,
                role=TurnRole.USER,
                content="Different",
                client_submission_id="client-1",
            ),
            expected_version=store.get_session(session.id).version,
        )


def test_fake_store_binds_one_active_task_and_checks_versions() -> None:
    store = FakeStore()
    session = store.create_session(Session(id="session-1", workspace_ref="workspace"))
    task = store.start_task(
        session.id,
        Task(id="task-1", goal="Inspect", repository="workspace"),
        expected_version=session.version,
    )
    with pytest.raises(LeaseConflict):
        store.start_task(
            session.id,
            Task(id="task-2", goal="Second", repository="workspace"),
            expected_version=store.get_session(session.id).version,
        )
    with pytest.raises(StaleVersion):
        store.update_task(task, expected_version=0)


def test_fake_store_effect_identity_and_lease_guard() -> None:
    store = FakeStore()
    session = store.create_session(Session(id="session-1", workspace_ref="workspace"))
    task = store.start_task(
        session.id,
        Task(id="task-1", goal="Inspect", repository="workspace"),
        expected_version=session.version,
    )
    execution = store.claim_execution(_execution(), expected_version=task.version)
    guard = LeaseGuard(execution.id, task.id, execution.lease_token, execution.generation)
    prepared = store.prepare_effects([_effect()], expected_version=store.get_task(task.id).version)
    assert prepared[0].id == "effect-1"
    assert (
        store.prepare_effects(
            [_effect()], expected_version=store.get_task(task.id).version, lease_guard=guard
        )[0].id
        == "effect-1"
    )
    with pytest.raises(EffectIdentityConflict):
        store.prepare_effects(
            [
                _effect(effect_id="effect-1", step_id="step-2").model_copy(
                    update={"arguments_summary": {"path": "other.py"}}
                )
            ],
            expected_version=store.get_task(task.id).version,
        )
    claimed = store.claim_effect("effect-1", expected_version=1, lease_guard=guard)
    committed = claimed.model_copy(update={"status": EffectStatus.SUCCEEDED})
    saved = store.commit_effect(
        committed,
        expected_version=claimed.version,
        result_ref="result-1",
        observation_ref="observation-1",
        lease_guard=guard,
    )
    assert saved.result_ref == "result-1"


def test_fake_store_recovery_requires_recovery_condition_and_disposition() -> None:
    store = FakeStore()
    store.create_session(Session(id="session-1", workspace_ref="workspace"))
    task = store.start_task(
        "session-1",
        Task(id="task-1", goal="Inspect", repository="workspace"),
        expected_version=1,
    )
    recovering = task.model_copy(
        update={
            "runtime_condition": TaskRuntimeCondition.RECOVERY_REQUIRED,
            "version": task.version,
        }
    )
    store.update_task(recovering, expected_version=task.version)
    current = store.get_task(task.id)
    disposition = RecoveryDisposition(
        unknown_effect_id="effect-1",
        kind=RecoveryDispositionKind.ABANDON,
        decision_source="operator",
    )
    resolved = store.resolve_recovery(
        disposition, task_id=task.id, expected_version=current.version
    )
    assert resolved.runtime_condition is TaskRuntimeCondition.ENDED
