"""SRF-03 step 7 recovery checks before execution/workspace takeover."""

from __future__ import annotations

import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from patchloop.domain import Task, TaskRuntimeCondition
from patchloop.execution.models import ExecutionStatus
from patchloop.execution.ownership import ExecutionOwnershipManager, LeasePolicy
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import RecoveryRequired
from patchloop.sandbox import (
    LocalProcessSandbox,
    ManagedCommandIdentity,
    ManagedCommandStatus,
)


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def _manager(
    store: SQLiteStore,
    clock: _Clock,
    execution_id: str,
) -> ExecutionOwnershipManager:
    return ExecutionOwnershipManager(
        store,
        clock=clock,
        id_factory=lambda: execution_id,
        policy=LeasePolicy(
            ttl=timedelta(seconds=1),
            heartbeat_interval=timedelta(milliseconds=200),
        ),
    )


def _acquire(
    manager: ExecutionOwnershipManager,
    task: Task,
    repository: Path,
    owner_id: str,
):
    return manager.acquire(
        session_id=task.session_id or "",
        task_id=task.id,
        owner_id=owner_id,
        repository=repository,
        expected_version=task.version,
        workspace_writer=True,
    )


def test_takeover_confirms_absent_command_before_new_writer(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    task = store.prepare_task_execution(
        Task(id="task-1", goal="Recover", repository=str(repository))
    )
    clock = _Clock()
    first = _acquire(_manager(store, clock, "execution-1"), task, repository, "worker-1")
    store.register_managed_command(
        ManagedCommandIdentity(
            id="command-1",
            execution_id=first.execution.id,
            backend="local",
            process_id=2_147_483_647,
            process_start_marker="absent-process-marker",
        ),
        lease_guard=first.lease_guard,
    )
    clock.now += timedelta(seconds=2)

    current = store.get_task(task.id)
    second = _acquire(_manager(store, clock, "execution-2"), current, repository, "worker-2")

    recovered = store.list_managed_commands("execution-1")[0]
    assert recovered.status is ManagedCommandStatus.TERMINATED
    assert recovered.cleanup_reason == "recovery_already_stopped"
    assert second.execution.generation == first.execution.generation + 1
    assert second.workspace_lease is not None


def test_takeover_terminates_verified_live_command_before_new_writer(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    task = store.prepare_task_execution(
        Task(id="task-1", goal="Recover live writer", repository=str(repository))
    )
    clock = _Clock()
    first = _acquire(_manager(store, clock, "execution-1"), task, repository, "worker-1")
    sandbox = LocalProcessSandbox()
    started = threading.Event()
    worker_errors: list[BaseException] = []
    sandbox.bind_execution(
        first.execution.id,
        command_started=lambda identity: (
            store.register_managed_command(identity, lease_guard=first.lease_guard),
            started.set(),
        ),
        command_finished=store.finish_managed_command,
        interruption_probe=lambda: None,
    )

    def run_old_command() -> None:
        try:
            sandbox.execute(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                repository,
                timeout_seconds=20,
                max_output_chars=1_000,
            )
        except BaseException as exc:
            worker_errors.append(exc)

    old_worker = threading.Thread(target=run_old_command)
    old_worker.start()
    assert started.wait(5)
    clock.now += timedelta(seconds=2)

    current = store.get_task(task.id)
    second = _acquire(_manager(store, clock, "execution-2"), current, repository, "worker-2")
    old_worker.join(timeout=5)

    assert not old_worker.is_alive()
    assert store.list_managed_commands("execution-1")[0].status in {
        ManagedCommandStatus.EXITED,
        ManagedCommandStatus.TERMINATED,
    }
    assert second.workspace_lease is not None


def test_unverifiable_cleanup_blocks_takeover_and_requires_recovery(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    task = store.prepare_task_execution(
        Task(id="task-1", goal="Block unsafe takeover", repository=str(repository))
    )
    clock = _Clock()
    first = _acquire(_manager(store, clock, "execution-1"), task, repository, "worker-1")
    store.register_managed_command(
        ManagedCommandIdentity(
            id="command-1",
            execution_id=first.execution.id,
            backend="unverifiable",
            process_id=2_147_483_647,
            process_start_marker="unknown",
        ),
        lease_guard=first.lease_guard,
    )
    clock.now += timedelta(seconds=2)

    with pytest.raises(RecoveryRequired) as raised:
        _acquire(
            _manager(store, clock, "execution-2"),
            store.get_task(task.id),
            repository,
            "worker-2",
        )

    assert raised.value.task_id == task.id
    assert store.get_task(task.id).runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED
    assert store.get_execution(first.execution.id).status is ExecutionStatus.RECOVERY_REQUIRED
    command = store.list_managed_commands(first.execution.id)[0]
    assert command.status is ManagedCommandStatus.CLEANUP_FAILED
    with pytest.raises(RecoveryRequired):
        _acquire(
            _manager(store, clock, "execution-3"),
            store.get_task(task.id),
            repository,
            "worker-3",
        )


def test_workspace_takeover_reconciles_command_from_another_task(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    first_task = store.prepare_task_execution(
        Task(id="task-1", goal="Old workspace writer", repository=str(repository))
    )
    second_task = store.prepare_task_execution(
        Task(id="task-2", goal="New workspace writer", repository=str(repository))
    )
    clock = _Clock()
    first = _acquire(
        _manager(store, clock, "execution-1"),
        first_task,
        repository,
        "worker-1",
    )
    store.register_managed_command(
        ManagedCommandIdentity(
            id="command-1",
            execution_id=first.execution.id,
            backend="local",
            process_id=2_147_483_647,
            process_start_marker="absent-process-marker",
        ),
        lease_guard=first.lease_guard,
    )
    clock.now += timedelta(seconds=2)

    second = _acquire(
        _manager(store, clock, "execution-2"),
        store.get_task(second_task.id),
        repository,
        "worker-2",
    )

    recovered = store.list_managed_commands(first.execution.id)[0]
    assert recovered.status is ManagedCommandStatus.TERMINATED
    assert recovered.cleanup_reason == "recovery_already_stopped"
    assert second.workspace_lease is not None
    assert second.workspace_lease.execution_id == second.execution.id


def test_workspace_recovery_failure_releases_new_execution(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    first_task = store.prepare_task_execution(
        Task(id="task-1", goal="Unverifiable old writer", repository=str(repository))
    )
    second_task = store.prepare_task_execution(
        Task(id="task-2", goal="Blocked new writer", repository=str(repository))
    )
    clock = _Clock()
    first = _acquire(
        _manager(store, clock, "execution-1"),
        first_task,
        repository,
        "worker-1",
    )
    store.register_managed_command(
        ManagedCommandIdentity(
            id="command-1",
            execution_id=first.execution.id,
            backend="unverifiable",
            process_id=2_147_483_647,
            process_start_marker="unknown",
        ),
        lease_guard=first.lease_guard,
    )
    clock.now += timedelta(seconds=2)

    with pytest.raises(RecoveryRequired) as raised:
        _acquire(
            _manager(store, clock, "execution-2"),
            store.get_task(second_task.id),
            repository,
            "worker-2",
        )

    assert raised.value.task_id == first_task.id
    assert store.get_task(first_task.id).runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED
    assert store.get_task(second_task.id).runtime_condition is TaskRuntimeCondition.IDLE
    assert store.get_execution("execution-2").status is ExecutionStatus.RELEASED
    assert store.list_managed_commands(first.execution.id)[0].status is (
        ManagedCommandStatus.CLEANUP_FAILED
    )


def test_late_finish_cannot_regress_a_terminal_managed_command(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    task = store.prepare_task_execution(
        Task(id="task-1", goal="Preserve terminal outcome", repository=str(repository))
    )
    lease = _acquire(
        _manager(store, _Clock(), "execution-1"), task, repository, "worker-1"
    )
    running = ManagedCommandIdentity(
        id="command-1",
        execution_id=lease.execution.id,
        backend="docker",
        process_id=123,
        process_start_marker="marker",
        container_name="patchloop-command-1",
        container_id="container-id-1234567890",
        docker_host="unix:///run/docker.sock",
    )
    store.register_managed_command(running, lease_guard=lease.lease_guard)
    terminated = running.model_copy(
        update={"status": ManagedCommandStatus.TERMINATED, "cleanup_reason": "takeover"}
    )
    store.finish_managed_command(terminated)

    late = running.model_copy(update={"status": ManagedCommandStatus.EXITED})
    persisted = store.finish_managed_command(late)

    assert persisted.status is ManagedCommandStatus.TERMINATED
    assert store.list_managed_commands(lease.execution.id)[0].status is (
        ManagedCommandStatus.TERMINATED
    )
