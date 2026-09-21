"""Real Docker acceptance cases invoked only after the SRF-07 backend probe passes."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from patchloop.domain import Task
from patchloop.execution.ownership import ExecutionOwnershipManager, LeasePolicy
from patchloop.persistence import SQLiteStore
from patchloop.sandbox import (
    DockerSandbox,
    ManagedCommandIdentity,
    ManagedCommandStatus,
    SandboxTimeoutError,
    _inspect_docker_container,
)


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def _manager(store: SQLiteStore, clock: _Clock, execution_id: str) -> ExecutionOwnershipManager:
    return ExecutionOwnershipManager(
        store,
        clock=clock,
        id_factory=lambda: execution_id,
        policy=LeasePolicy(
            ttl=timedelta(seconds=1),
            heartbeat_interval=timedelta(milliseconds=200),
        ),
    )


def test_docker_timeout_terminates_process_tree(tmp_path: Path) -> None:
    marker = tmp_path / "child-survived.txt"
    child_code = (
        "import pathlib,time; time.sleep(2); "
        "pathlib.Path('/workspace/child-survived.txt').write_text('survived')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(20)"
    )
    sandbox = DockerSandbox()
    finished = []
    sandbox.bind_execution(
        "docker-timeout-execution",
        command_started=lambda identity: None,
        command_finished=finished.append,
        interruption_probe=lambda: None,
    )

    with pytest.raises(SandboxTimeoutError):
        sandbox.execute(
            ["python", "-c", parent_code],
            tmp_path,
            timeout_seconds=0.5,
            max_output_chars=1_000,
        )
    time.sleep(2.2)

    assert not marker.exists()
    workloads = [item for item in finished if item.purpose == "workload"]
    preflights = [item for item in finished if item.purpose == "preflight"]
    assert len(workloads) == len(preflights) == 1
    assert workloads[0].status is ManagedCommandStatus.TERMINATED
    assert workloads[0].cleanup_reason == "timeout"
    assert workloads[0].container_name is not None
    inspected = subprocess.run(
        ["docker", "inspect", workloads[0].container_name],
        capture_output=True,
        text=True,
        check=False,
    )
    assert inspected.returncode != 0


def test_docker_registration_failure_never_starts_workload(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-run.txt"
    observed: list[ManagedCommandIdentity] = []
    sandbox = DockerSandbox()

    def reject_workload(identity: ManagedCommandIdentity) -> None:
        observed.append(identity)
        if identity.purpose == "workload":
            raise RuntimeError("registration rejected")

    sandbox.bind_execution(
        "docker-registration-gate",
        command_started=reject_workload,
        command_finished=lambda identity: None,
        interruption_probe=lambda: None,
    )

    with pytest.raises(RuntimeError, match="registration rejected"):
        sandbox.execute(
            [
                "python",
                "-c",
                "from pathlib import Path; "
                "Path('/workspace/must-not-run.txt').write_text('ran')",
            ],
            tmp_path,
            timeout_seconds=10,
            max_output_chars=1_000,
        )

    workload = next(item for item in observed if item.purpose == "workload")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _inspect_docker_container(
        workload.container_id or ""
    ) is not None:
        time.sleep(0.05)
    assert not marker.exists()
    assert _inspect_docker_container(workload.container_id or "") is None


def test_docker_user_exit_125_is_preserved(tmp_path: Path) -> None:
    finished: list[ManagedCommandIdentity] = []
    sandbox = DockerSandbox()
    sandbox.bind_execution(
        "docker-user-exit-125",
        command_started=lambda identity: None,
        command_finished=finished.append,
        interruption_probe=lambda: None,
    )

    result = sandbox.execute(
        ["python", "-c", "raise SystemExit(125)"],
        tmp_path,
        timeout_seconds=10,
        max_output_chars=1_000,
    )

    workloads = [item for item in finished if item.purpose == "workload"]
    assert result.exit_code == 125
    assert len(workloads) == 1
    assert workloads[0].status is ManagedCommandStatus.EXITED
    assert _inspect_docker_container(workloads[0].container_id or "") is None


def test_docker_old_worker_is_stopped_before_lease_takeover(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    task = store.prepare_task_execution(
        Task(id="task-1", goal="Recover Docker worker", repository=str(repository))
    )
    clock = _Clock()
    first_manager = _manager(store, clock, "execution-1")
    first = first_manager.acquire(
        session_id=task.session_id or "",
        task_id=task.id,
        owner_id="worker-1",
        repository=repository,
        expected_version=task.version,
        workspace_writer=True,
    )
    sandbox = DockerSandbox()
    started = threading.Event()
    worker_errors: list[BaseException] = []
    def register(identity) -> None:
        store.register_managed_command(identity, lease_guard=first.lease_guard)
        if identity.purpose == "workload":
            started.set()

    sandbox.bind_execution(
        first.execution.id,
        command_started=register,
        command_finished=store.finish_managed_command,
        interruption_probe=lambda: None,
    )

    def run_old_command() -> None:
        try:
            sandbox.execute(
                [sys.executable, "-c", "import time; time.sleep(20)"],
                repository,
                timeout_seconds=30,
                max_output_chars=1_000,
            )
        except BaseException as exc:
            worker_errors.append(exc)

    old_worker = threading.Thread(target=run_old_command)
    old_worker.start()
    assert started.wait(10)
    clock.now += timedelta(seconds=2)

    current = store.get_task(task.id)
    second = _manager(store, clock, "execution-2").acquire(
        session_id=current.session_id or "",
        task_id=current.id,
        owner_id="worker-2",
        repository=repository,
        expected_version=current.version,
        workspace_writer=True,
    )
    old_worker.join(timeout=10)

    assert not old_worker.is_alive()
    assert worker_errors == []
    recovered = next(
        item
        for item in store.list_managed_commands(first.execution.id)
        if item.purpose == "workload"
    )
    assert recovered.status in {ManagedCommandStatus.EXITED, ManagedCommandStatus.TERMINATED}
    assert second.execution.generation == first.execution.generation + 1
    assert second.workspace_lease is not None
    assert first.workspace_lease is not None
    assert second.workspace_lease.generation == first.workspace_lease.generation + 1
