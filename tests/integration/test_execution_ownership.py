"""SRF-03 step 1: ordered Session, Task, and Workspace ownership."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import pytest

from patchloop.domain import Task, TaskRuntimeCondition
from patchloop.execution.models import ExecutionStatus
from patchloop.execution.ownership import (
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_LEASE_TTL,
    ExecutionOwnershipManager,
    LeasePolicy,
    workspace_id_for,
)
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import FakeStore, LeaseConflict, LeaseLost
from patchloop.session.models import Session


class _OwnershipTestStore(Protocol):
    def create_session(self, session: Session) -> Session: ...

    def start_task(self, session_id: str, task: Task, *, expected_version: int) -> Task: ...

    def get_task(self, task_id: str) -> Task: ...


class _MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 6, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def _start_task(
    store: _OwnershipTestStore, session_id: str, task_id: str, repository: Path
) -> Task:
    session = store.create_session(Session(id=session_id, workspace_ref=str(repository)))
    return store.start_task(
        session.id,
        Task(id=task_id, goal="Own the workspace", repository=str(repository)),
        expected_version=session.version,
    )


@pytest.fixture(params=["fake", "sqlite"])
def ownership_store(request: pytest.FixtureRequest, tmp_path: Path) -> FakeStore | SQLiteStore:
    if request.param == "fake":
        return FakeStore()
    return SQLiteStore(tmp_path / "lease-contract.db")


def _manager(
    store: FakeStore | SQLiteStore,
    clock: _MutableClock,
    *,
    ids: list[str],
    tokens: list[str],
) -> ExecutionOwnershipManager:
    id_values = iter(ids)
    token_values = iter(tokens)
    return ExecutionOwnershipManager(
        store,
        clock=clock,
        id_factory=lambda: next(id_values),
        token_factory=lambda: next(token_values),
    )


def test_execution_claim_reserves_session_and_task_slot(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "ownership.db")
    task = _start_task(store, "session-1", "task-1", repository)
    manager = ExecutionOwnershipManager(store)

    first = manager.acquire(
        session_id="session-1",
        task_id="task-1",
        owner_id="worker-1",
        repository=repository,
        expected_version=task.version,
        workspace_writer=False,
    )

    with pytest.raises(LeaseConflict) as conflict:
        manager.acquire(
            session_id="session-1",
            task_id="task-1",
            owner_id="worker-2",
            repository=repository,
            expected_version=store.get_task(task.id).version + 99,
            workspace_writer=False,
        )

    assert conflict.value.resource_id == "session-1"
    assert first.execution.status is ExecutionStatus.RUNNING


def test_invalid_workspace_fails_before_claiming_execution(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "ownership.db")
    task = _start_task(store, "session-1", "task-1", repository)
    manager = ExecutionOwnershipManager(store)

    with pytest.raises(FileNotFoundError):
        manager.acquire(
            session_id="session-1",
            task_id="task-1",
            owner_id="worker-1",
            repository=tmp_path / "missing",
            expected_version=task.version,
        )

    assert _execution_ids(store.path) == []
    assert store.get_task(task.id).runtime_condition is TaskRuntimeCondition.IDLE


def test_workspace_alias_contends_and_failure_releases_execution(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    nested = repository / "nested"
    nested.mkdir(parents=True)
    alias = nested / ".."
    store = SQLiteStore(tmp_path / "ownership.db")
    first_task = _start_task(store, "session-1", "task-1", repository)
    second_task = _start_task(store, "session-2", "task-2", alias)
    manager = ExecutionOwnershipManager(store)

    first = manager.acquire(
        session_id="session-1",
        task_id="task-1",
        owner_id="worker-1",
        repository=repository,
        expected_version=first_task.version,
    )
    assert first.workspace_lease is not None
    assert first.workspace_lease.workspace_id == workspace_id_for(alias)
    assert first.workspace_lease.lease_token != first.execution.lease_token

    with pytest.raises(LeaseConflict) as conflict:
        manager.acquire(
            session_id="session-2",
            task_id="task-2",
            owner_id="worker-2",
            repository=alias,
            expected_version=second_task.version,
        )

    assert conflict.value.resource_id == workspace_id_for(repository)
    released_execution = store.get_execution(
        next(
            execution_id
            for execution_id in _execution_ids(store.path)
            if execution_id != first.execution.id
        )
    )
    assert released_execution.status is ExecutionStatus.RELEASED
    released_task = store.get_task(second_task.id)
    assert released_task.runtime_condition is TaskRuntimeCondition.IDLE

    read_only = manager.acquire(
        session_id="session-2",
        task_id="task-2",
        owner_id="worker-2",
        repository=alias,
        expected_version=released_task.version,
        workspace_writer=False,
    )
    assert read_only.workspace_lease is None


def test_different_workspaces_have_independent_writers(tmp_path: Path) -> None:
    first_repository = tmp_path / "first"
    second_repository = tmp_path / "second"
    first_repository.mkdir()
    second_repository.mkdir()
    store = SQLiteStore(tmp_path / "ownership.db")
    first_task = _start_task(store, "session-1", "task-1", first_repository)
    second_task = _start_task(store, "session-2", "task-2", second_repository)
    manager = ExecutionOwnershipManager(store)

    first = manager.acquire(
        session_id="session-1",
        task_id="task-1",
        owner_id="worker-1",
        repository=first_repository,
        expected_version=first_task.version,
    )
    second = manager.acquire(
        session_id="session-2",
        task_id="task-2",
        owner_id="worker-2",
        repository=second_repository,
        expected_version=second_task.version,
    )

    assert first.workspace_lease is not None
    assert second.workspace_lease is not None
    assert first.workspace_lease.workspace_id != second.workspace_lease.workspace_id


def test_default_lease_policy_pins_ttl_and_heartbeat() -> None:
    policy = LeasePolicy()

    assert policy.ttl == DEFAULT_LEASE_TTL == timedelta(seconds=60)
    assert policy.heartbeat_interval == DEFAULT_HEARTBEAT_INTERVAL == timedelta(seconds=20)
    with pytest.raises(ValueError, match="shorter"):
        LeasePolicy(ttl=timedelta(seconds=20), heartbeat_interval=timedelta(seconds=20))


def test_renew_and_assert_use_injected_clock(
    ownership_store: FakeStore | SQLiteStore, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(exist_ok=True)
    task = _start_task(ownership_store, "session-1", "task-1", repository)
    clock = _MutableClock()
    manager = _manager(
        ownership_store,
        clock,
        ids=["execution-1"],
        tokens=["execution-token-1", "workspace-token-1"],
    )
    ownership = manager.acquire(
        session_id="session-1",
        task_id="task-1",
        owner_id="worker-1",
        repository=repository,
        expected_version=task.version,
    )

    clock.advance(40)
    renewed = manager.renew(ownership)
    assert renewed.execution.lease_expires_at == clock.now + timedelta(seconds=60)
    assert renewed.workspace_lease is not None
    assert renewed.workspace_lease.lease_expires_at == renewed.execution.lease_expires_at
    clock.advance(59)
    manager.assert_owned(renewed)
    clock.advance(2)
    with pytest.raises(LeaseLost):
        manager.assert_owned(renewed)


def test_expired_owner_is_fenced_by_takeover_with_new_generation(
    ownership_store: FakeStore | SQLiteStore, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(exist_ok=True)
    task = _start_task(ownership_store, "session-1", "task-1", repository)
    clock = _MutableClock()
    first_manager = _manager(
        ownership_store,
        clock,
        ids=["execution-1"],
        tokens=["execution-token-1", "workspace-token-1"],
    )
    first = first_manager.acquire(
        session_id="session-1",
        task_id="task-1",
        owner_id="worker-1",
        repository=repository,
        expected_version=task.version,
    )

    clock.advance(61)
    second_manager = _manager(
        ownership_store,
        clock,
        ids=["execution-2"],
        tokens=["execution-token-2", "workspace-token-2"],
    )
    second = second_manager.acquire(
        session_id="session-1",
        task_id="task-1",
        owner_id="worker-2",
        repository=repository,
        expected_version=ownership_store.get_task(task.id).version,
    )

    assert second.execution.generation == first.execution.generation + 1
    assert second.workspace_lease is not None
    assert first.workspace_lease is not None
    assert second.workspace_lease.generation == first.workspace_lease.generation + 1
    assert second.execution.lease_token != first.execution.lease_token
    assert second.workspace_lease.lease_token != first.workspace_lease.lease_token
    with pytest.raises(LeaseLost):
        first_manager.assert_owned(first)
    with pytest.raises(LeaseLost):
        first_manager.release(first)
    second_manager.assert_owned(second)


def test_owner_is_part_of_renew_release_and_assert_guard(
    ownership_store: FakeStore | SQLiteStore, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(exist_ok=True)
    task = _start_task(ownership_store, "session-1", "task-1", repository)
    clock = _MutableClock()
    manager = _manager(
        ownership_store,
        clock,
        ids=["execution-1"],
        tokens=["execution-token-1", "workspace-token-1"],
    )
    ownership = manager.acquire(
        session_id="session-1",
        task_id="task-1",
        owner_id="worker-1",
        repository=repository,
        expected_version=task.version,
    )
    wrong_execution_guard = replace(ownership.lease_guard, owner_id="worker-2")
    assert ownership.workspace_guard is not None
    wrong_workspace_guard = replace(ownership.workspace_guard, owner_id="worker-2")

    with pytest.raises(LeaseLost):
        ownership_store.assert_execution(wrong_execution_guard, now=clock.now)
    with pytest.raises(LeaseLost):
        ownership_store.renew_execution(
            wrong_execution_guard,
            now=clock.now,
            lease_expires_at=clock.now + timedelta(seconds=60),
        )
    with pytest.raises(LeaseLost):
        ownership_store.release_execution(wrong_execution_guard, now=clock.now)
    with pytest.raises(LeaseLost):
        ownership_store.assert_workspace_writer(wrong_workspace_guard, now=clock.now)
    with pytest.raises(LeaseLost):
        ownership_store.renew_workspace_writer(
            wrong_workspace_guard,
            now=clock.now,
            lease_expires_at=clock.now + timedelta(seconds=60),
        )
    with pytest.raises(LeaseLost):
        ownership_store.release_workspace_writer(wrong_workspace_guard, now=clock.now)
    manager.assert_owned(ownership)


def test_release_preserves_fencing_generation_for_next_owner(
    ownership_store: FakeStore | SQLiteStore, tmp_path: Path
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(exist_ok=True)
    task = _start_task(ownership_store, "session-1", "task-1", repository)
    clock = _MutableClock()
    first_manager = _manager(
        ownership_store,
        clock,
        ids=["execution-1"],
        tokens=["execution-token-1", "workspace-token-1"],
    )
    first = first_manager.acquire(
        session_id="session-1",
        task_id="task-1",
        owner_id="worker-1",
        repository=repository,
        expected_version=task.version,
    )
    first_manager.release(first)
    with pytest.raises(LeaseLost):
        first_manager.assert_owned(first)

    second_manager = _manager(
        ownership_store,
        clock,
        ids=["execution-2"],
        tokens=["execution-token-2", "workspace-token-2"],
    )
    released_task = ownership_store.get_task(task.id)
    second = second_manager.acquire(
        session_id="session-1",
        task_id="task-1",
        owner_id="worker-2",
        repository=repository,
        expected_version=released_task.version,
    )

    assert second.execution.generation == first.execution.generation + 1
    assert second.workspace_lease is not None
    assert first.workspace_lease is not None
    assert second.workspace_lease.generation == first.workspace_lease.generation + 1


def test_lease_events_are_safe_and_actionable(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "ownership.db")
    task = _start_task(store, "session-1", "task-1", repository)
    clock = _MutableClock()
    first_manager = _manager(
        store,
        clock,
        ids=["execution-1"],
        tokens=["execution-token-1", "workspace-token-1"],
    )
    first = first_manager.acquire(
        session_id="session-1",
        task_id="task-1",
        owner_id="sensitive-worker-name",
        repository=repository,
        expected_version=task.version,
    )

    clock.advance(61)
    with pytest.raises(LeaseLost):
        first_manager.renew(first)

    events = store.list_events("session-1")
    lease_events = [event for event in events if event.type.startswith("lease.")]
    assert [event.type for event in lease_events[:2]] == [
        "lease.acquired",
        "lease.acquired",
    ]
    assert [event.type for event in lease_events[-2:]] == [
        "lease.renew_failed",
        "lease.lost",
    ]
    assert all(event.data["owner_summary"].startswith("sha256:") for event in lease_events)
    serialized = "\n".join(event.model_dump_json() for event in lease_events)
    assert "sensitive-worker-name" not in serialized
    assert "execution-token-1" not in serialized
    assert "workspace-token-1" not in serialized
    assert all(event.data["recovery_advice"] for event in lease_events)


def test_contention_release_and_takeover_events(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "ownership.db")
    first_task = _start_task(store, "session-1", "task-1", repository)
    second_task = _start_task(store, "session-2", "task-2", repository)
    clock = _MutableClock()
    first_manager = _manager(
        store,
        clock,
        ids=["execution-1"],
        tokens=["execution-token-1", "workspace-token-1"],
    )
    first = first_manager.acquire(
        session_id="session-1",
        task_id="task-1",
        owner_id="worker-1",
        repository=repository,
        expected_version=first_task.version,
    )
    second_manager = _manager(
        store,
        clock,
        ids=["execution-2", "execution-3"],
        tokens=[
            "execution-token-2",
            "workspace-token-2",
            "execution-token-3",
            "workspace-token-3",
        ],
    )

    with pytest.raises(LeaseConflict):
        second_manager.acquire(
            session_id="session-2",
            task_id="task-2",
            owner_id="worker-2",
            repository=repository,
            expected_version=second_task.version,
        )

    second_event_types = [event.type for event in store.list_events("session-2")]
    assert "lease.contended" in second_event_types
    assert "lease.released" in second_event_types

    clock.advance(61)
    second = second_manager.acquire(
        session_id="session-2",
        task_id="task-2",
        owner_id="worker-2",
        repository=repository,
        expected_version=store.get_task(second_task.id).version,
    )

    assert second.workspace_lease is not None
    assert "lease.takeover" in [event.type for event in store.list_events("session-2")]
    with pytest.raises(LeaseLost):
        first_manager.assert_owned(first)


def _execution_ids(database: Path) -> list[str]:
    import sqlite3

    with sqlite3.connect(database) as connection:
        return [str(row[0]) for row in connection.execute("SELECT id FROM executions ORDER BY id")]
