"""SRF-02 step 4 conditional writes and atomic Effect commits."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from patchloop.domain import Task, TaskStatus, ToolCall, ToolResult
from patchloop.events import SessionEvent
from patchloop.execution.models import Approval, Effect, EffectStatus, Execution
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import FakeStore, LeaseGuard, StaleVersion
from patchloop.security import CredentialBinding
from patchloop.session.models import Session, SessionCheckpoint, Turn, TurnRole


@pytest.fixture(params=["fake", "sqlite"])
def runtime_store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[object]:
    if request.param == "fake":
        yield FakeStore()
    else:
        yield SQLiteStore(tmp_path / "runtime-contract.db")


def _execution() -> Execution:
    return Execution(
        id="execution-1",
        session_id="session-1",
        task_id="task-1",
        owner_id="worker-1",
        lease_token="secret-token",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )


def _effect() -> Effect:
    return Effect(
        id="effect-1",
        task_id="task-1",
        step_id="step-1",
        batch_position=0,
        provider_call_id="provider-call-1",
        tool_name="write_file",
        action_kind="write",
        arguments_summary={"path": "example.py"},
    )


def _owned_effect(store: object) -> tuple[LeaseGuard, Effect]:
    session = store.create_session(Session(id="session-1", workspace_ref="workspace"))
    task = store.start_task(
        session.id,
        Task(id="task-1", goal="Edit", repository="workspace"),
        expected_version=session.version,
    )
    execution = store.claim_execution(_execution(), expected_version=task.version)
    guard = LeaseGuard(
        execution.id,
        task.id,
        execution.lease_token,
        execution.generation,
        execution.owner_id,
    )
    effect = store.prepare_effects(
        [_effect()], expected_version=store.get_task(task.id).version, lease_guard=guard
    )[0]
    return guard, store.claim_effect(effect.id, expected_version=effect.version, lease_guard=guard)


def test_create_and_update_task_are_conditional(runtime_store: object) -> None:
    task = runtime_store.create_task(Task(id="task-1", goal="First", repository="workspace"))
    with pytest.raises(ValueError, match="already exists"):
        runtime_store.create_task(Task(id="task-1", goal="Replacement", repository="workspace"))
    with pytest.raises(StaleVersion):
        runtime_store.update_task(task, expected_version=task.version + 1)

    running = task.model_copy(update={"status": TaskStatus.RUNNING})
    updated = runtime_store.update_task(running, expected_version=task.version)
    assert updated.version == task.version + 1
    assert runtime_store.get_task(task.id).goal == "First"


def test_commit_tool_result_is_idempotent_but_rejects_conflicts(runtime_store: object) -> None:
    task = runtime_store.create_task(Task(id="task-1", goal="Inspect", repository="workspace"))
    call = ToolCall(id="call-1", name="read_file", arguments={"path": "a.py"})
    result = ToolResult(call_id=call.id, tool_name=call.name, success=True, output="ok")

    assert (
        runtime_store.commit_tool_result(task.id, call, result, expected_version=task.version)
        == result
    )
    updated = runtime_store.update_task(task, expected_version=task.version)
    assert (
        runtime_store.commit_tool_result(task.id, call, result, expected_version=task.version)
        == result
    )
    assert updated.version == task.version + 1
    with pytest.raises(ValueError, match="different content"):
        runtime_store.commit_tool_result(
            task.id,
            call,
            result.model_copy(update={"output": "changed"}),
            expected_version=task.version,
        )


def test_append_event_allocates_a_session_cursor(runtime_store: object) -> None:
    created = runtime_store.create_session(Session(id="session-1", workspace_ref="workspace"))
    requested = SessionEvent(id="event-1", session_id="session-1", type="session.created")
    first = runtime_store.append_event(
        requested,
        expected_sequence=created.event_sequence,
    )
    second = runtime_store.append_event(
        SessionEvent(id="event-2", session_id="session-1", type="turn.appended"),
        expected_sequence=first.sequence,
    )

    assert [first.sequence, second.sequence] == [2, 3]
    assert [event.id for event in runtime_store.list_events("session-1")][1:] == [
        "event-1",
        "event-2",
    ]
    assert runtime_store.append_event(requested, expected_sequence=0) == first
    with pytest.raises(StaleVersion):
        runtime_store.append_event(
            SessionEvent(id="event-3", session_id="session-1", type="stale"),
            expected_sequence=0,
        )


def test_closed_session_rejects_new_turns(runtime_store: object) -> None:
    session = runtime_store.create_session(Session(id="session-1", workspace_ref="workspace"))
    runtime_store.close_session(session.id, expected_version=session.version)

    with pytest.raises(ValueError, match="closed session"):
        runtime_store.append_turn(
            Turn(session_id=session.id, role=TurnRole.USER, content="too late")
        )


def test_approval_decision_is_conditional_and_idempotent(runtime_store: object) -> None:
    _guard, _claimed = _owned_effect(runtime_store)
    pending = Approval(
        id="approval-1",
        effect_id="effect-1",
        action_summary="Write example.py",
        policy_version="policy-1",
        config_version="config-1",
    )
    assert runtime_store.decide_approval(pending).status.value == "pending"
    decided = pending.decide(approved=True, source="operator")
    assert (
        runtime_store.decide_approval(decided, expected_version=pending.version).status.value
        == "approved"
    )
    assert runtime_store.decide_approval(decided).status.value == "approved"


def test_commit_effect_advances_event_and_checkpoint_atomically(runtime_store: object) -> None:
    guard, claimed = _owned_effect(runtime_store)
    task = runtime_store.get_task(claimed.task_id)
    runtime_store.commit_checkpoint(
        SessionCheckpoint(session_id="session-1", task_id=task.id),
        expected_version=task.version,
        lease_guard=guard,
    )

    committed = runtime_store.commit_effect(
        claimed.model_copy(update={"status": EffectStatus.SUCCEEDED}),
        expected_version=claimed.version,
        result_ref="result-1",
        observation_ref="observation-1",
        lease_guard=guard,
    )

    assert committed.version == claimed.version + 1
    assert committed.result_ref == "result-1"
    event = next(
        event
        for event in runtime_store.list_events("session-1")
        if event.type == "effect.committed"
    )
    assert event.type == "effect.committed"
    assert event.data["effect_id"] == committed.id
    assert runtime_store.get_session_checkpoint(task.id).event_sequence == event.sequence


def test_checkpoint_cursors_and_effects_must_be_backed_by_session_facts(
    runtime_store: object,
) -> None:
    guard, claimed = _owned_effect(runtime_store)
    task = runtime_store.get_task(claimed.task_id)
    session = runtime_store.get_session("session-1")

    with pytest.raises(ValueError, match="event cursor"):
        runtime_store.commit_checkpoint(
            SessionCheckpoint(
                session_id=session.id,
                task_id=task.id,
                event_sequence=session.event_sequence + 1,
            ),
            expected_version=task.version,
            lease_guard=guard,
        )
    with pytest.raises(ValueError, match="input cursor"):
        runtime_store.commit_checkpoint(
            SessionCheckpoint(
                session_id=session.id,
                task_id=task.id,
                consumed_input_sequence=1,
            ),
            expected_version=task.version,
            lease_guard=guard,
        )
    with pytest.raises(ValueError, match="outside its task"):
        runtime_store.commit_checkpoint(
            SessionCheckpoint(
                session_id=session.id,
                task_id=task.id,
                pending_effect_ids=["provider-invented-effect"],
            ),
            expected_version=task.version,
            lease_guard=guard,
        )

    committed = runtime_store.commit_checkpoint(
        SessionCheckpoint(
            session_id=session.id,
            task_id=task.id,
            event_sequence=session.event_sequence,
            pending_effect_ids=[claimed.id],
        ),
        expected_version=task.version,
        lease_guard=guard,
    )
    assert committed.pending_effect_ids == [claimed.id]


def test_sqlite_effect_persists_credential_reference_without_secret(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "credential-reference.db")
    session = store.create_session(Session(id="session-1", workspace_ref="workspace"))
    task = store.start_task(
        session.id,
        Task(id="task-1", goal="Call service", repository="workspace"),
        expected_version=session.version,
    )
    execution = store.claim_execution(_execution(), expected_version=task.version)
    guard = LeaseGuard(
        execution.id,
        task.id,
        execution.lease_token,
        execution.generation,
        execution.owner_id,
    )
    effect = _effect().model_copy(
        update={
            "arguments_summary": {"password": "raw-secret", "path": "example.py"},
            "credential_bindings": [
                CredentialBinding(path="/password", reference="credential://vault/service-password")
            ],
        }
    )

    persisted = store.prepare_effects(
        [effect], expected_version=store.get_task(task.id).version, lease_guard=guard
    )[0]

    assert persisted.arguments_summary["password"] == {
        "$credential_ref": "credential://vault/service-password"
    }
    assert persisted.credential_bindings == effect.credential_bindings
    assert b"raw-secret" not in store.path.read_bytes()


class _FailingEventStore(SQLiteStore):
    fail_events = False

    def _insert_event_row(self, connection: sqlite3.Connection, event: SessionEvent) -> None:
        if self.fail_events:
            raise RuntimeError("injected event failure")
        super()._insert_event_row(connection, event)


def test_turn_append_rolls_back_when_its_journal_event_fails(tmp_path: Path) -> None:
    store = _FailingEventStore(tmp_path / "turn-rollback.db")
    session = store.create_session(Session(id="session-1", workspace_ref="workspace"))
    before = store.get_session(session.id)
    store.fail_events = True

    with pytest.raises(RuntimeError, match="injected event failure"):
        store.append_turn(Turn(session_id=session.id, role=TurnRole.USER, content="not committed"))

    assert store.list_turns(session.id) == []
    assert store.get_session(session.id) == before


def test_commit_effect_rolls_back_when_event_insert_fails(tmp_path: Path) -> None:
    store = _FailingEventStore(tmp_path / "rollback.db")
    # Enable the injected failure only after setup has written its journal.
    store.fail_events = False
    guard, claimed = _owned_effect(store)
    before = store.list_events("session-1")
    store.fail_events = True

    with pytest.raises(RuntimeError, match="injected event failure"):
        store.commit_effect(
            claimed.model_copy(update={"status": EffectStatus.SUCCEEDED}),
            expected_version=claimed.version,
            result_ref="result-1",
            observation_ref="observation-1",
            lease_guard=guard,
        )

    persisted = store.get_effect(claimed.id)
    assert persisted.status is EffectStatus.EXECUTING
    assert persisted.result_ref is None
    assert store.list_events("session-1") == before
