"""SRF-02 step 4 conditional writes and atomic Effect commits."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from patchloop.domain import (
    ErrorKind,
    Task,
    TaskRuntimeCondition,
    TaskStatus,
    ToolCall,
    ToolResult,
)
from patchloop.events import SessionEvent
from patchloop.execution.approvals import build_approval
from patchloop.execution.models import (
    Approval,
    ApprovalStatus,
    Effect,
    EffectStatus,
    Execution,
    RecoveryDisposition,
    RecoveryDispositionKind,
)
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import (
    FakeStore,
    LeaseConflict,
    LeaseGuard,
    StaleVersion,
)
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


def _owned_prepared_effect(store: object) -> tuple[LeaseGuard, Effect]:
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
    return guard, effect


def _owned_effect(store: object) -> tuple[LeaseGuard, Effect]:
    guard, effect = _owned_prepared_effect(store)
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


def test_claim_effect_consumes_exact_approval_once(runtime_store: object) -> None:
    session = runtime_store.create_session(Session(id="session-1", workspace_ref="workspace"))
    task = runtime_store.start_task(
        session.id,
        Task(id="task-1", goal="Edit", repository="workspace"),
        expected_version=session.version,
    )
    execution = runtime_store.claim_execution(_execution(), expected_version=task.version)
    guard = LeaseGuard(
        execution.id,
        task.id,
        execution.lease_token,
        execution.generation,
        execution.owner_id,
    )
    prepared = runtime_store.prepare_effects(
        [
            _effect().model_copy(
                update={
                    "policy_result": {
                        "decision": "require_approval",
                        "approval_required": True,
                    }
                }
            )
        ],
        expected_version=runtime_store.get_task(task.id).version,
        lease_guard=guard,
    )[0]
    request = build_approval(
        runtime_store.get_task(task.id),
        prepared,
        policy_version="policy-1",
        config_version="1",
    )
    waiting, pending, _ = runtime_store.request_effect_approval(
        prepared.id,
        request,
        expected_version=prepared.version,
        lease_guard=guard,
    )
    decided, authorized, _ = runtime_store.resolve_effect_approval(
        pending.id,
        approved=True,
        source="operator",
        expected_version=pending.version,
        workspace_ref="workspace",
        policy_version="policy-1",
        config_version="1",
    )

    claimed = runtime_store.claim_effect(
        authorized.id,
        expected_version=authorized.version,
        lease_guard=guard,
        effect_fingerprint=authorized.content_fingerprint(),
        workspace_ref="workspace",
        policy_version="policy-1",
        config_version="1",
        policy_decision="require_approval",
    )

    assert waiting.status is EffectStatus.WAITING_FOR_APPROVAL
    assert decided.status is ApprovalStatus.APPROVED
    assert claimed.status is EffectStatus.EXECUTING
    assert claimed.approval_id is None
    assert claimed.approval_consumed is True
    assert runtime_store.get_approval(pending.id).status is ApprovalStatus.CONSUMED
    with pytest.raises(LeaseConflict):
        runtime_store.claim_effect(
            claimed.id,
            expected_version=claimed.version,
            lease_guard=guard,
        )


def test_commit_effect_advances_event_and_checkpoint_atomically(runtime_store: object) -> None:
    guard, claimed = _owned_effect(runtime_store)
    task = runtime_store.get_task(claimed.task_id)
    call = ToolCall(
        id=claimed.provider_call_id,
        name=claimed.tool_name,
        arguments=claimed.arguments_summary,
    )
    result = ToolResult(
        call_id=call.id,
        tool_name=call.name,
        success=True,
        output="written",
    )
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
        call=call,
        result=result,
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
    assert runtime_store.get_tool_result(task.id, call.id) == result


@pytest.mark.parametrize("status", [EffectStatus.DENIED, EffectStatus.CANCELLED])
def test_settle_unexecuted_effect_pairs_provider_observation(
    runtime_store: object,
    status: EffectStatus,
) -> None:
    guard, prepared = _owned_prepared_effect(runtime_store)
    call = ToolCall(
        id=prepared.provider_call_id,
        name=prepared.tool_name,
        arguments=prepared.arguments_summary,
    )
    result = ToolResult(
        call_id=call.id,
        tool_name=call.name,
        success=False,
        error_kind=ErrorKind.PERMISSION_DENIED,
        output=f'{{"backend_invoked": false, "effect_status": "{status.value}"}}',
    )

    settled = runtime_store.settle_unexecuted_effect(
        prepared.id,
        status=status,
        expected_version=prepared.version,
        call=call,
        result=result,
        lease_guard=guard,
    )

    assert settled.status is status
    assert settled.result_ref == f"tool-result:{prepared.task_id}:{call.id}"
    assert runtime_store.get_tool_result(prepared.task_id, call.id) == result
    event = next(
        event for event in runtime_store.list_events("session-1") if event.type == "effect.settled"
    )
    assert event.data == {"effect_id": prepared.id, "status": status.value}


def test_mark_effect_unknown_requires_task_recovery(runtime_store: object) -> None:
    guard, claimed = _owned_effect(runtime_store)
    evidence = {"source": "result_missing", "tool_name": claimed.tool_name}

    unknown, task = runtime_store.mark_effect_unknown(
        claimed.id,
        expected_version=claimed.version,
        evidence=evidence,
        lease_guard=guard,
    )

    assert unknown.status is EffectStatus.UNKNOWN
    assert unknown.reconciliation_evidence == evidence
    assert task.runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED
    assert runtime_store.get_effect(claimed.id) == unknown
    assert runtime_store.get_task(task.id) == task
    event = next(
        event
        for event in runtime_store.list_events("session-1")
        if event.type == "effect.recovery_required"
    )
    assert event.data["effect_id"] == claimed.id


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
    call = ToolCall(
        id=claimed.provider_call_id,
        name=claimed.tool_name,
        arguments=claimed.arguments_summary,
    )
    result = ToolResult(
        call_id=call.id,
        tool_name=call.name,
        success=True,
        output="written",
    )
    store.fail_events = True

    with pytest.raises(RuntimeError, match="injected event failure"):
        store.commit_effect(
            claimed.model_copy(update={"status": EffectStatus.SUCCEEDED}),
            expected_version=claimed.version,
            result_ref="result-1",
            observation_ref="observation-1",
            lease_guard=guard,
            call=call,
            result=result,
        )

    persisted = store.get_effect(claimed.id)
    assert persisted.status is EffectStatus.EXECUTING
    assert persisted.result_ref is None
    assert store.get_tool_result(claimed.task_id, call.id) is None
    assert store.list_events("session-1") == before


def test_mark_effect_unknown_rolls_back_when_event_insert_fails(tmp_path: Path) -> None:
    store = _FailingEventStore(tmp_path / "reconcile-rollback.db")
    guard, claimed = _owned_effect(store)
    before_task = store.get_task(claimed.task_id)
    before_events = store.list_events("session-1")
    store.fail_events = True

    with pytest.raises(RuntimeError, match="injected event failure"):
        store.mark_effect_unknown(
            claimed.id,
            expected_version=claimed.version,
            evidence={"source": "result_missing"},
            lease_guard=guard,
        )

    assert store.get_effect(claimed.id) == claimed
    assert store.get_task(claimed.task_id) == before_task
    assert store.list_events("session-1") == before_events


def test_settle_unexecuted_effect_rolls_back_when_event_insert_fails(tmp_path: Path) -> None:
    store = _FailingEventStore(tmp_path / "settle-effect-rollback.db")
    guard, prepared = _owned_prepared_effect(store)
    call = ToolCall(
        id=prepared.provider_call_id,
        name=prepared.tool_name,
        arguments=prepared.arguments_summary,
    )
    result = ToolResult(
        call_id=call.id,
        tool_name=call.name,
        success=False,
        error_kind=ErrorKind.PERMISSION_DENIED,
        output="denied without execution",
    )
    before_events = store.list_events("session-1")
    store.fail_events = True

    with pytest.raises(RuntimeError, match="injected event failure"):
        store.settle_unexecuted_effect(
            prepared.id,
            status=EffectStatus.DENIED,
            expected_version=prepared.version,
            call=call,
            result=result,
            lease_guard=guard,
        )

    assert store.get_effect(prepared.id) == prepared
    assert store.get_tool_result(prepared.task_id, call.id) is None
    assert store.list_events("session-1") == before_events


def test_confirm_recovery_rolls_back_when_event_insert_fails(tmp_path: Path) -> None:
    store = _FailingEventStore(tmp_path / "recovery-resolution-rollback.db")
    guard, claimed = _owned_effect(store)
    unknown, recovery_task = store.mark_effect_unknown(
        claimed.id,
        expected_version=claimed.version,
        evidence={"source": "result_missing"},
        lease_guard=guard,
    )
    call = ToolCall(
        id=unknown.provider_call_id,
        name=unknown.tool_name,
        arguments=unknown.arguments_summary,
    )
    result = ToolResult(
        call_id=call.id,
        tool_name=call.name,
        success=True,
        output="verified",
    )
    disposition = RecoveryDisposition(
        id="recovery-1",
        unknown_effect_id=unknown.id,
        kind=RecoveryDispositionKind.CONFIRM_RESULT,
        evidence={"verified_by": "operator"},
        decision_source="operator",
    )
    before_events = store.list_events("session-1")
    store.fail_events = True

    with pytest.raises(RuntimeError, match="injected event failure"):
        store.resolve_recovery(
            disposition,
            task_id=recovery_task.id,
            expected_version=recovery_task.version,
            confirmed_call=call,
            confirmed_result=result,
        )

    assert store.get_effect(unknown.id) == unknown
    assert store.get_task(recovery_task.id) == recovery_task
    assert store.get_tool_result(recovery_task.id, call.id) is None
    with pytest.raises(KeyError, match="recovery disposition not found"):
        store.get_recovery_disposition(disposition.id)
    assert store.list_events("session-1") == before_events
