"""Runtime integration for managed command cancellation and lease-loss cleanup."""

from __future__ import annotations

import multiprocessing
import os
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from patchloop.domain import Task, TaskRuntimeCondition, TaskStatus, ToolCall
from patchloop.execution.models import (
    ControlKind,
    ControlRequest,
    ControlStatus,
    ExecutionStatus,
)
from patchloop.execution.ownership import ExecutionOwnershipManager, LeasePolicy
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import LeaseLost
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.runtime import AgentRuntime
from patchloop.sandbox import (
    LocalProcessSandbox,
    ManagedCommandStatus,
    SandboxCleanupError,
)
from patchloop.sqlite_support import connect
from patchloop.tools import PermissionLevel, RunTestsTool, ToolContext, ToolGateway, ToolPolicy


def _gateway(repository: Path, sandbox: LocalProcessSandbox) -> ToolGateway:
    return ToolGateway(
        ToolContext(repository, sandbox),
        [RunTestsTool()],
        policy=ToolPolicy(
            frozenset({PermissionLevel.EXECUTE}),
            require_plan_for_mutations=False,
        ),
    )


def _provider() -> FakeProvider:
    return FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="slow-tests",
                        name="run_tests",
                        arguments={
                            "command": [sys.executable, "-m", "pytest", "-q", "test_slow.py"],
                            "timeout_seconds": 30,
                        },
                    )
                ]
            ),
            ModelResponse(content="done"),
        ]
    )


def _wait_for_active_command(store: SQLiteStore, execution_id: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if store.list_managed_commands(execution_id, active_only=True):
            return
        time.sleep(0.02)
    raise AssertionError("managed command did not start")


def _crashing_sandbox_parent(repository: str, marker: str, started) -> None:
    sandbox = LocalProcessSandbox()
    sandbox.bind_execution(
        "crashing-execution",
        command_started=lambda identity: started.set(),
        command_finished=lambda identity: None,
        interruption_probe=lambda: None,
    )
    child_code = (
        f"import pathlib,time; time.sleep(1); pathlib.Path({marker!r}).write_text('survived')"
    )
    sandbox.execute(
        [sys.executable, "-c", child_code],
        Path(repository),
        timeout_seconds=10,
        max_output_chars=1_000,
    )


@pytest.mark.parametrize("control_kind", [ControlKind.PAUSE, ControlKind.CANCEL])
def test_runtime_control_terminates_command_before_settling(
    tmp_path: Path, control_kind: ControlKind
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(10)\n",
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / "state.db")
    manager = ExecutionOwnershipManager(store, id_factory=lambda: "execution-1")
    runtime = AgentRuntime(
        _provider(),
        _gateway(repository, LocalProcessSandbox()),
        state_store=store,
        ownership_manager=manager,
        owner_id="worker-1",
    )
    result: list[Task] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            result.append(
                runtime.run(Task(id="task-1", goal="Run slow tests", repository=str(repository)))
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    _wait_for_active_command(store, "execution-1")
    if control_kind is ControlKind.CANCEL:
        store.cancel_task("task-1")
    else:
        task = store.get_task("task-1")
        store.request_pause(
            ControlRequest(
                task_id=task.id,
                execution_id="execution-1",
                kind=ControlKind.PAUSE,
            ),
            expected_version=task.version,
        )
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert errors == []
    command = store.list_managed_commands("execution-1")[0]
    assert command.status is ManagedCommandStatus.TERMINATED
    assert command.cleanup_reason == control_kind.value
    assert store.list_managed_commands("execution-1", active_only=True) == []
    with connect(store.path) as connection:
        row = connection.execute(
            "SELECT payload_json FROM control_requests WHERE task_id = ?", ("task-1",)
        ).fetchone()
    control = ControlRequest.model_validate_json(row["payload_json"])
    assert control.status is ControlStatus.SETTLED
    assert control.settled_at is not None
    assert command.updated_at <= control.settled_at
    assert result
    if control_kind is ControlKind.CANCEL:
        assert result[0].status is TaskStatus.CANCELLED
    else:
        assert result[0].runtime_condition is TaskRuntimeCondition.PAUSED


class _RenewFailsDuringCommandStore(SQLiteStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.command_started = threading.Event()

    def register_managed_command(self, identity, *, lease_guard):
        registered = super().register_managed_command(identity, lease_guard=lease_guard)
        self.command_started.set()
        return registered

    def renew_execution(self, lease_guard, *, now, lease_expires_at):
        if self.command_started.is_set():
            raise LeaseLost(lease_guard.task_id)
        return super().renew_execution(
            lease_guard,
            now=now,
            lease_expires_at=lease_expires_at,
        )


class _ReportingCleanupFailureSandbox(LocalProcessSandbox):
    def _terminate_process(self, process, identity) -> None:
        process.kill()
        process.wait(timeout=5)
        raise SandboxCleanupError("injected cleanup confirmation failure")


class _RuntimeExitCleanupFailureSandbox(LocalProcessSandbox):
    def terminate_all(self, reason: str) -> None:
        raise SandboxCleanupError(f"injected {reason} cleanup confirmation failure")


def test_lease_loss_terminates_command_without_result_or_checkpoint_advance(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(10)\n",
        encoding="utf-8",
    )
    store = _RenewFailsDuringCommandStore(tmp_path / "state.db")
    manager = ExecutionOwnershipManager(
        store,
        id_factory=lambda: "execution-1",
        policy=LeasePolicy(
            ttl=timedelta(milliseconds=500),
            heartbeat_interval=timedelta(milliseconds=50),
        ),
    )

    with pytest.raises(LeaseLost):
        AgentRuntime(
            _provider(),
            _gateway(repository, LocalProcessSandbox()),
            state_store=store,
            ownership_manager=manager,
            owner_id="worker-1",
        ).run(Task(id="task-1", goal="Lose ownership", repository=str(repository)))

    command = store.list_managed_commands("execution-1")[0]
    assert command.status is ManagedCommandStatus.TERMINATED
    assert command.cleanup_reason == "lease_lost"
    assert store.get_tool_result("task-1", "slow-tests") is None
    assert store.get_checkpoint("task-1").next_step_index == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object acceptance test")
def test_windows_job_kills_command_when_runtime_parent_crashes(tmp_path: Path) -> None:
    marker = tmp_path / "command-survived.txt"
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    parent = context.Process(
        target=_crashing_sandbox_parent,
        args=(str(tmp_path), str(marker), started),
    )
    parent.start()
    assert started.wait(10)

    parent.terminate()
    parent.join(timeout=10)
    assert not parent.is_alive()
    time.sleep(1.2)

    assert not marker.exists()


def test_cleanup_failure_does_not_settle_cancel_or_release_execution(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(10)\n",
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / "state.db")
    runtime = AgentRuntime(
        _provider(),
        _gateway(repository, _ReportingCleanupFailureSandbox()),
        state_store=store,
        ownership_manager=ExecutionOwnershipManager(store, id_factory=lambda: "execution-1"),
        owner_id="worker-1",
    )
    errors: list[BaseException] = []

    def run() -> None:
        try:
            runtime.run(Task(id="task-1", goal="Fail cleanup", repository=str(repository)))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    _wait_for_active_command(store, "execution-1")
    store.cancel_task("task-1")
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], SandboxCleanupError)
    assert store.get_task("task-1").status is TaskStatus.RUNNING
    assert store.get_task("task-1").runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED
    assert store.get_execution("execution-1").status is ExecutionStatus.RUNNING
    command = store.list_managed_commands("execution-1")[0]
    assert command.status is ManagedCommandStatus.CLEANUP_FAILED
    with connect(store.path) as connection:
        row = connection.execute(
            "SELECT payload_json FROM control_requests WHERE task_id = ?", ("task-1",)
        ).fetchone()
    control = ControlRequest.model_validate_json(row["payload_json"])
    assert control.status is ControlStatus.CLEANUP_FAILED
    assert control.cleanup_info


def test_runtime_exit_cleanup_failure_requires_recovery_without_control(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    runtime = AgentRuntime(
        FakeProvider([ModelResponse(content="done")]),
        _gateway(repository, _RuntimeExitCleanupFailureSandbox()),
        state_store=store,
        ownership_manager=ExecutionOwnershipManager(store, id_factory=lambda: "execution-1"),
        owner_id="worker-1",
    )

    with pytest.raises(SandboxCleanupError, match="runtime_exit"):
        runtime.run(Task(id="task-1", goal="Fail exit cleanup", repository=str(repository)))

    task = store.get_task("task-1")
    assert task.status is TaskStatus.COMPLETED
    assert task.runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED
    assert store.get_execution("execution-1").status is ExecutionStatus.RUNNING
