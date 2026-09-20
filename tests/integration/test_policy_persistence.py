import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from patchloop.domain import Task
from patchloop.execution.approvals import ApprovalService, build_approval
from patchloop.execution.models import Approval, Effect
from patchloop.execution.ownership import ExecutionOwnership, ExecutionOwnershipManager
from patchloop.execution.policy import (
    ActionDescriptor,
    ApprovalScopeKind,
    PolicyAction,
    ResourceKind,
    ResourceSelector,
    digest,
)
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import ApprovalConflict, FakeStore, LeaseLost, StaleVersion
from patchloop.security import RiskLevel
from patchloop.session.models import Session
from patchloop.sqlite_support import connect, connect_write, runtime_schema_version


def _compete_grant(
    store: SQLiteStore | FakeStore, grant_id: str, action_json: str, barrier: Any, revoke: bool
) -> str:
    barrier.wait(timeout=45)
    try:
        if revoke:
            store.revoke_grant(grant_id, source="race", expected_version=1)
            return "revoked"
        store.consume_grant(
            grant_id,
            ActionDescriptor.model_validate_json(action_json),
            expected_version=1,
            policy_version="p1",
            config_version="1",
        )
        return "consumed"
    except (StaleVersion, ApprovalConflict):
        return "conflict"


def _grant_process(
    database: str, grant_id: str, action_json: str, barrier: Any, results: Any, revoke: bool
) -> None:
    try:
        results.put(
            _compete_grant(SQLiteStore(Path(database)), grant_id, action_json, barrier, revoke)
        )
    except Exception as exc:
        results.put(f"unexpected:{type(exc).__name__}:{exc}")


@pytest.fixture(params=["sqlite", "fake"])
def prepared(
    request: pytest.FixtureRequest, tmp_path: Path
) -> tuple[SQLiteStore | FakeStore, Approval, ActionDescriptor]:
    store = SQLiteStore(tmp_path / "state.db") if request.param == "sqlite" else FakeStore()
    session = store.create_session(Session(workspace_ref=str(tmp_path)))
    task = store.start_task(
        session.id, Task(goal="edit", repository=str(tmp_path)), expected_version=session.version
    )
    owner = ExecutionOwnershipManager(store).acquire(
        session_id=session.id,
        task_id=task.id,
        owner_id="test",
        repository=tmp_path,
        expected_version=task.version,
        workspace_writer=True,
    )
    arguments = {"path": "a.txt", "content": "hello"}
    action = ActionDescriptor(
        action=PolicyAction.EDIT,
        tool_name="write_file",
        workspace_ref=str(tmp_path),
        session_id=session.id,
        resources=(ResourceSelector(kind=ResourceKind.PATH, value="a.txt"),),
        arguments_fingerprint=digest(arguments),
        side_effect=True,
        risk=RiskLevel.MEDIUM,
    )
    effect = Effect(
        task_id=task.id,
        step_id="step",
        batch_position=0,
        provider_call_id="call",
        tool_name="write_file",
        action_kind="write",
        arguments_summary=arguments,
        arguments_fingerprint=action.arguments_fingerprint,
        action_descriptor=action,
        policy_result={"decision": "require_approval"},
    )
    effect = store.prepare_effects(
        [effect], expected_version=store.get_task(task.id).version, lease_guard=owner.lease_guard
    )[0]
    approval = build_approval(
        store.get_task(task.id), effect, policy_version="p1", config_version="1"
    )
    store.request_effect_approval(
        effect.id, approval, expected_version=effect.version, lease_guard=owner.lease_guard
    )
    return store, approval, action


def test_scope_creation_consumption_revocation_and_events(
    prepared: tuple[SQLiteStore | FakeStore, Approval, ActionDescriptor],
) -> None:
    store, approval, action = prepared
    service = ApprovalService(store)
    decided, _, _ = service.decide_current(
        approval.id, approved=True, source="operator", scope_kind=ApprovalScopeKind.SESSION
    )
    grant = store.get_grant(decided.grant_id or "")
    assert grant.matches(action, "p1", "1")
    consumed = store.consume_grant(
        grant.id, action, expected_version=grant.version, policy_version="p1", config_version="1"
    )
    assert consumed.version == grant.version + 1
    with pytest.raises(StaleVersion):
        store.consume_grant(
            grant.id,
            action,
            expected_version=grant.version,
            policy_version="p1",
            config_version="1",
        )
    revoked = service.revoke_grant(grant.id, source="operator", reason="password=hidden")
    assert revoked.status == "revoked"
    with pytest.raises(ApprovalConflict):
        store.consume_grant(
            grant.id,
            action,
            expected_version=revoked.version,
            policy_version="p1",
            config_version="1",
        )
    events = store.list_events(action.session_id)
    assert {"approval.grant_created", "approval.grant_consumed", "approval.grant_revoked"} <= {
        event.type for event in events
    }
    assert "hidden" not in "".join(event.model_dump_json() for event in events)


def test_resource_requires_expiry_and_duplicate_cannot_expand_scope(
    prepared: tuple[SQLiteStore | FakeStore, Approval, ActionDescriptor],
) -> None:
    store, approval, action = prepared
    service = ApprovalService(store)
    with pytest.raises(ValueError, match="expiry"):
        service.decide_current(
            approval.id, approved=True, source="operator", scope_kind=ApprovalScopeKind.RESOURCE
        )
    assert store.get_approval(approval.id).status == "pending"
    assert store.list_grants(action.workspace_ref) == []
    expires = datetime.now(UTC) + timedelta(hours=1)
    decided, _, _ = service.decide_current(
        approval.id,
        approved=True,
        source="operator",
        scope_kind=ApprovalScopeKind.RESOURCE,
        expires_at=expires,
    )
    grant = store.get_grant(decided.grant_id or "")
    assert grant.matches(action.model_copy(update={"session_id": "other"}), "p1", "1")
    assert not grant.matches(action.model_copy(update={"workspace_ref": "other"}), "p1", "1")
    with pytest.raises(ApprovalConflict, match="scope_mismatch"):
        service.decide_current(approval.id, approved=True, source="operator")


def test_lazy_expiry_is_persisted_once(
    prepared: tuple[SQLiteStore | FakeStore, Approval, ActionDescriptor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import patchloop.execution.models as models
    import patchloop.execution.policy as policy

    store, approval, action = prepared
    expires = datetime.now(UTC) + timedelta(hours=1)
    decided, _, _ = ApprovalService(store).decide_current(
        approval.id,
        approved=True,
        source="operator",
        scope_kind=ApprovalScopeKind.RESOURCE,
        expires_at=expires,
    )

    class FutureClock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return expires + timedelta(seconds=1)

    monkeypatch.setattr(policy, "datetime", FutureClock)
    monkeypatch.setattr(models, "_now", lambda: expires + timedelta(seconds=1))
    expired = store.get_grant(decided.grant_id or "")
    assert expired.status == "expired"
    assert store.get_grant(expired.id).version == expired.version
    assert store.get_approval(approval.id).status == "expired"
    with pytest.raises(ApprovalConflict):
        store.consume_grant(
            expired.id,
            action,
            expected_version=expired.version,
            policy_version="p1",
            config_version="1",
        )
    assert (
        sum(event.type == "approval.expired" for event in store.list_events(action.session_id)) == 2
    )


def test_v5_database_upgrades_to_v6_without_changing_legacy_approval_defaults(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.db"
    SQLiteStore(path)
    with connect_write(path) as connection:
        connection.execute("DROP TABLE policy_rules")
        connection.execute("DROP TABLE approval_grants")
        connection.execute(
            "UPDATE patchloop_schema_migrations SET version = 5 WHERE component = 'runtime'"
        )
    restored = SQLiteStore(path)
    with connect(restored.path) as connection:
        assert runtime_schema_version(connection) == 6
    legacy = Approval(
        effect_id="effect", action_summary="write", policy_version="p1", config_version="1"
    )
    assert legacy.scope_kind == ApprovalScopeKind.ONCE
    assert legacy.grant_id is None
    assert legacy.expires_at is None


def test_cli_scope_grant_list_explain_and_revoke(
    prepared: tuple[SQLiteStore | FakeStore, Approval, ActionDescriptor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from typer.testing import CliRunner

    import patchloop.cli as cli

    store, approval, action = prepared
    monkeypatch.setattr(cli, "_sqlite_store", lambda repository: store)
    runner = CliRunner()
    prefix = ["approval", "--repo", action.workspace_ref]
    result = runner.invoke(
        cli.app, [*prefix, "decide", approval.id, "--approve", "--scope", "session"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "1.0"
    decided = store.get_approval(approval.id)
    assert decided.scope_kind == ApprovalScopeKind.SESSION
    listed = runner.invoke(cli.app, [*prefix, "grant", "list", action.session_id])
    assert listed.exit_code == 0, listed.output
    assert decided.grant_id in listed.output
    explained = runner.invoke(
        cli.app, ["policy", "--repo", action.workspace_ref, "explain", approval.effect_id]
    )
    assert explained.exit_code == 0, explained.output
    assert "a.txt" in explained.output
    human = runner.invoke(cli.app, [*prefix, "--human", "grant", "list", action.session_id])
    assert human.exit_code == 0, human.output
    assert "scope=session" in human.output
    revoked = runner.invoke(
        cli.app,
        [*prefix, "grant", "revoke", decided.grant_id or "", "--reason", "no longer needed"],
    )
    assert revoked.exit_code == 0, revoked.output
    assert store.get_grant(decided.grant_id or "").status == "revoked"


@pytest.mark.parametrize("with_revoker", [False, True])
def test_eight_workers_consume_or_revoke_finite_grant_atomically(
    prepared: tuple[SQLiteStore | FakeStore, Approval, ActionDescriptor],
    with_revoker: bool,
) -> None:
    store, approval, action = prepared
    decided, _, _ = ApprovalService(store).decide_current(
        approval.id, approved=True, source="operator", scope_kind=ApprovalScopeKind.SESSION
    )
    grant = store.get_grant(decided.grant_id or "").model_copy(update={"remaining_uses": 1})
    if isinstance(store, SQLiteStore):
        with connect_write(store.path) as connection:
            store._write_grant(connection, grant)
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(9)
        results = context.Queue()
        processes = [
            context.Process(
                target=_grant_process,
                args=(
                    str(store.path),
                    grant.id,
                    action.model_dump_json(),
                    barrier,
                    results,
                    with_revoker and index == 0,
                ),
            )
            for index in range(8)
        ]
        try:
            for process in processes:
                process.start()
            barrier.wait(timeout=45)
            outcomes = [results.get(timeout=45) for _ in processes]
            for process in processes:
                process.join(timeout=45)
                assert process.exitcode == 0
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            results.close()
            results.join_thread()
    else:
        store.grants[grant.id] = grant
        barrier = Barrier(9)
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(
                    _compete_grant,
                    store,
                    grant.id,
                    action.model_dump_json(),
                    barrier,
                    with_revoker and index == 0,
                )
                for index in range(8)
            ]
            barrier.wait(timeout=45)
            outcomes = [future.result(timeout=45) for future in futures]
    assert outcomes.count("conflict") == 7, outcomes
    assert outcomes.count("consumed") + outcomes.count("revoked") == 1
    terminal = store.get_grant(grant.id)
    assert terminal.version == 2
    assert terminal.status in {"exhausted", "revoked"}
    events = store.list_events(action.session_id)
    assert (
        sum(event.type in {"approval.grant_consumed", "approval.grant_revoked"} for event in events)
        == 1
    )


def test_released_owner_cannot_consume_grant_on_claim(
    prepared: tuple[SQLiteStore | FakeStore, Approval, ActionDescriptor],
) -> None:
    store, approval, action = prepared
    decided, effect, task = ApprovalService(store).decide_current(
        approval.id, approved=True, source="operator", scope_kind=ApprovalScopeKind.SESSION
    )
    old = ExecutionOwnership(execution=store.list_executions(task.id)[0])
    store.release_execution(old.lease_guard, now=datetime.now(UTC))
    current = ExecutionOwnershipManager(store).acquire(
        session_id=action.session_id,
        task_id=task.id,
        owner_id="new-owner",
        repository=Path(action.workspace_ref),
        expected_version=store.get_task(task.id).version,
        workspace_writer=False,
    )
    with pytest.raises(LeaseLost):
        store.claim_effect(
            effect.id,
            expected_version=effect.version,
            lease_guard=old.lease_guard,
            policy_version="p1",
            config_version="1",
        )
    assert store.get_grant(decided.grant_id or "").version == 1
    claimed = store.claim_effect(
        effect.id,
        expected_version=effect.version,
        lease_guard=current.lease_guard,
        policy_version="p1",
        config_version="1",
    )
    assert claimed.consumed_grant_id == decided.grant_id
    assert store.get_grant(decided.grant_id or "").version == 2


def test_approval_replacement_preserves_history_and_rejects_stale_submission(
    prepared: tuple[SQLiteStore | FakeStore, Approval, ActionDescriptor],
) -> None:
    store, approval, action = prepared
    decided, original, task = ApprovalService(store).decide_current(
        approval.id, approved=True, source="operator", scope_kind=ApprovalScopeKind.SESSION
    )
    old = ExecutionOwnership(execution=store.list_executions(task.id)[0])
    store.release_execution(old.lease_guard, now=datetime.now(UTC))
    owner = ExecutionOwnershipManager(store).acquire(
        session_id=action.session_id,
        task_id=task.id,
        owner_id="replace-owner",
        repository=Path(action.workspace_ref),
        expected_version=store.get_task(task.id).version,
        workspace_writer=False,
    )
    arguments = {"path": "a.txt", "content": "changed"}
    replacement = original.model_copy(
        update={
            "id": "replacement",
            "step_id": "replacement-step",
            "supersedes_effect_id": original.id,
            "approval_id": None,
            "version": 1,
            "arguments_summary": arguments,
            "arguments_fingerprint": digest(arguments),
            "action_descriptor": action.model_copy(
                update={"arguments_fingerprint": digest(arguments)}
            ),
        }
    )
    request = build_approval(
        store.get_task(task.id),
        replacement,
        policy_version="p1",
        config_version="1",
        supersedes_approval_id=decided.id,
    )
    with pytest.raises(StaleVersion):
        store.replace_effect_approval(
            original.id,
            replacement,
            request,
            expected_version=original.version - 1,
            lease_guard=owner.lease_guard,
        )
    assert len(store.list_effects(task.id)) == 1
    waiting, new_approval, updated = store.replace_effect_approval(
        original.id,
        replacement,
        request,
        expected_version=original.version,
        lease_guard=owner.lease_guard,
    )
    assert waiting.status == "waiting_for_approval"
    assert updated.runtime_condition == "waiting_for_approval"
    assert store.get_effect(original.id).status == "cancelled"
    assert store.get_approval(decided.id).status == "expired"
    assert store.get_grant(decided.grant_id or "").status == "revoked"
    assert new_approval.supersedes_approval_id == decided.id


def test_direct_grant_consumption_rechecks_deny_rules(prepared):
    from patchloop.execution.policy import PolicyRule, PolicyRuleEffect

    store, approval, action = prepared
    resolution = ApprovalService(store).decide_current(
        approval.id, approved=True, source="operator", scope_kind=ApprovalScopeKind.SESSION
    )
    assert resolution.grant is not None
    assert resolution.grant.id == resolution.approval.grant_id
    grant = resolution.grant
    store.put_policy_rule(
        PolicyRule(
            id="deny-new",
            source="project",
            workspace_ref=action.workspace_ref,
            action=action.action,
            resource_kind=ResourceKind.PATH,
            pattern="a.txt",
            effect=PolicyRuleEffect.DENY,
            policy_version="p1",
        )
    )
    with pytest.raises(ApprovalConflict, match="grant_policy_mismatch"):
        store.consume_grant(
            grant.id,
            action,
            expected_version=grant.version,
            policy_version="p1",
            config_version="1",
        )
    assert store.get_grant(grant.id).version == grant.version
    assert not any(
        e.type == "approval.grant_consumed" for e in store.list_events(action.session_id)
    )
