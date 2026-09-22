"""Real Docker acceptance cases invoked only after the SRF-07 backend probe passes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from patchloop.domain import (
    Task,
    TaskExecutionConfig,
    TaskRuntimeCondition,
    ToolCall,
)
from patchloop.execution.ownership import ExecutionOwnershipManager, LeasePolicy
from patchloop.persistence import SQLiteStore
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.runtime import AgentRuntime
from patchloop.sandbox import (
    DockerSandbox,
    DockerSandboxConfig,
    ManagedCommandIdentity,
    ManagedCommandStatus,
    SandboxTimeoutError,
    _inspect_docker_container,
)
from patchloop.tools import PermissionLevel, ReplaceTextTool, ToolContext, ToolGateway, ToolPolicy


class _InterruptAfterReplaceGateway(ToolGateway):
    def execute_claimed(
        self,
        task_id: str,
        call: ToolCall,
        *,
        approval_consumed: bool,
    ):
        result = super().execute_claimed(
            task_id,
            call,
            approval_consumed=approval_consumed,
        )
        if call.name == "replace_text":
            raise KeyboardInterrupt("crash after file mutation")
        return result


def _file_gateway(repository: Path, *, interrupt: bool) -> ToolGateway:
    gateway_type = _InterruptAfterReplaceGateway if interrupt else ToolGateway
    return gateway_type(
        ToolContext(repository),
        [ReplaceTextTool()],
        policy=ToolPolicy(
            frozenset({PermissionLevel.WRITE}),
            approval_threshold=None,
            require_plan_for_mutations=False,
        ),
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
            ttl=timedelta(seconds=60),
            heartbeat_interval=timedelta(seconds=20),
        ),
    )


def _sandbox() -> DockerSandbox:
    image = os.environ.get("PATCHLOOP_ACCEPTANCE_DOCKER_IMAGE", "patchloop-sandbox:py313")
    return DockerSandbox(DockerSandboxConfig(image=image))


def _assert_target_container(identity: ManagedCommandIdentity, repository: Path) -> None:
    expected = os.environ.get("PATCHLOOP_ACCEPTANCE_DOCKER_IMAGE_ID")
    inspected = subprocess.run(
        ["docker", "inspect", identity.container_id or ""],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert inspected.returncode == 0, inspected.stderr
    payload = json.loads(inspected.stdout)[0]
    if expected is not None:
        assert payload["Image"] == expected
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        assert payload["Config"]["User"] == f"{os.getuid()}:{os.getgid()}"
    workspace_mounts = [
        mount for mount in payload["Mounts"] if mount.get("Destination") == "/workspace"
    ]
    assert len(workspace_mounts) == 1
    assert Path(workspace_mounts[0]["Source"]).resolve() == repository.resolve()


def test_docker_timeout_terminates_process_tree(tmp_path: Path) -> None:
    control = tmp_path / "write-control.txt"
    control_sandbox = _sandbox()
    control_sandbox.bind_execution(
        "docker-timeout-control",
        command_started=lambda identity: None,
        command_finished=lambda identity: None,
        interruption_probe=lambda: None,
    )
    control_result = control_sandbox.execute(
        [
            "python",
            "-c",
            "from pathlib import Path; Path('/workspace/write-control.txt').write_text('ok')",
        ],
        tmp_path,
        timeout_seconds=10,
        max_output_chars=1_000,
    )
    assert control_result.exit_code == 0
    assert control.read_text(encoding="utf-8") == "ok"

    marker = tmp_path / "child-survived.txt"
    started_marker = tmp_path / "timeout-started.txt"
    child_code = (
        "import pathlib,time; time.sleep(2); "
        "pathlib.Path('/workspace/child-survived.txt').write_text('survived')"
    )
    parent_code = (
        "import pathlib,subprocess,sys,time; "
        "pathlib.Path('/workspace/timeout-started.txt').write_text('started'); "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(20)"
    )
    sandbox = _sandbox()
    finished = []
    started: list[ManagedCommandIdentity] = []

    def record_started(identity: ManagedCommandIdentity) -> None:
        started.append(identity)
        if identity.purpose == "workload":
            _assert_target_container(identity, tmp_path)

    sandbox.bind_execution(
        "docker-timeout-execution",
        command_started=record_started,
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
    assert len([item for item in started if item.purpose == "workload"]) == 1
    assert started_marker.read_text(encoding="utf-8") == "started"
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
    sandbox = _sandbox()

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
                "from pathlib import Path; Path('/workspace/must-not-run.txt').write_text('ran')",
            ],
            tmp_path,
            timeout_seconds=10,
            max_output_chars=1_000,
        )

    workload = next(item for item in observed if item.purpose == "workload")
    deadline = time.monotonic() + 5
    while (
        time.monotonic() < deadline
        and _inspect_docker_container(workload.container_id or "") is not None
    ):
        time.sleep(0.05)
    assert not marker.exists()
    assert _inspect_docker_container(workload.container_id or "") is None


def test_docker_user_exit_125_is_preserved(tmp_path: Path) -> None:
    finished: list[ManagedCommandIdentity] = []
    sandbox = _sandbox()
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
    sandbox = _sandbox()
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
    second = None
    cleanup_errors: list[BaseException] = []
    try:
        if not started.wait(20):
            pytest.fail(f"Docker workload was not registered: {worker_errors!r}")
        workload = next(
            item
            for item in store.list_managed_commands(first.execution.id)
            if item.purpose == "workload"
        )
        _assert_target_container(workload, repository)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            snapshot = _inspect_docker_container(workload.container_id or "")
            if snapshot is not None and snapshot.state.get("Running") is True:
                break
            time.sleep(0.05)
        else:
            pytest.fail("Docker workload never reached State.Running")
        clock.now += timedelta(seconds=61)

        current = store.get_task(task.id)
        second = _manager(store, clock, "execution-2").acquire(
            session_id=current.session_id or "",
            task_id=current.id,
            owner_id="worker-2",
            repository=repository,
            expected_version=current.version,
            workspace_writer=True,
        )
    finally:
        if old_worker.is_alive():
            try:
                sandbox.terminate_all("acceptance-finally")
            except BaseException as exc:
                cleanup_errors.append(exc)
        old_worker.join(timeout=10)

    assert not old_worker.is_alive()
    assert cleanup_errors == []
    assert worker_errors == []
    recovered = next(
        item
        for item in store.list_managed_commands(first.execution.id)
        if item.purpose == "workload"
    )
    assert recovered.status in {ManagedCommandStatus.EXITED, ManagedCommandStatus.TERMINATED}
    assert second is not None
    assert second.execution.generation == first.execution.generation + 1
    assert second.workspace_lease is not None
    assert first.workspace_lease is not None
    assert second.workspace_lease.generation == first.workspace_lease.generation + 1


def test_docker_recovery_preserves_user_modified_file(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    target = repository / "README.md"
    target.write_text("# Before\n", encoding="utf-8")
    marker = repository / "effect-started.txt"
    store = SQLiteStore(tmp_path / "state.db")
    image = os.environ.get("PATCHLOOP_ACCEPTANCE_DOCKER_IMAGE", "patchloop-sandbox:py313")
    task = Task(
        id="task-docker-user-edit",
        goal="Preserve the user's edit during recovery",
        repository=str(repository),
        execution=TaskExecutionConfig(sandbox_backend="docker", sandbox_image=image),
    )
    with pytest.raises(KeyboardInterrupt, match="file mutation"):
        AgentRuntime(
            FakeProvider(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="docker-file-effect",
                                name="replace_text",
                                arguments={
                                    "path": "README.md",
                                    "old_text": "Before",
                                    "new_text": "After",
                                },
                            )
                        ]
                    )
                ]
            ),
            _file_gateway(repository, interrupt=True),
            state_store=store,
            owner_id="effect-worker-before-crash",
        ).run(task)
    interrupted = next(
        effect
        for effect in store.list_effects(task.id)
        if effect.provider_call_id == "docker-file-effect"
    )
    assert store.get_tool_result(task.id, "docker-file-effect") is None

    task = store.get_task(task.id)
    clock = _Clock()
    first = _manager(store, clock, "execution-user-edit-1").acquire(
        session_id=task.session_id or "",
        task_id=task.id,
        owner_id="worker-before-crash",
        repository=repository,
        expected_version=task.version,
        workspace_writer=True,
    )
    sandbox = _sandbox()
    workload_started = threading.Event()
    errors: list[BaseException] = []

    def register(identity: ManagedCommandIdentity) -> None:
        store.register_managed_command(identity, lease_guard=first.lease_guard)
        if identity.purpose == "workload":
            workload_started.set()

    sandbox.bind_execution(
        first.execution.id,
        command_started=register,
        command_finished=store.finish_managed_command,
        interruption_probe=lambda: None,
    )

    def side_effect_then_wait() -> None:
        try:
            sandbox.execute(
                [
                    "python",
                    "-c",
                    "from pathlib import Path; import time; "
                    "Path('/workspace/README.md').write_text('# After\\n'); "
                    "Path('/workspace/effect-started.txt').write_text('started'); "
                    "time.sleep(60)",
                ],
                repository,
                timeout_seconds=90,
                max_output_chars=1_000,
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=side_effect_then_wait)
    worker.start()
    second = None
    cleanup_errors: list[BaseException] = []
    try:
        if not workload_started.wait(20):
            pytest.fail(f"Docker side-effect workload was not registered: {errors!r}")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.05)
        assert marker.read_text(encoding="utf-8") == "started"
        workload = next(
            item
            for item in store.list_managed_commands(first.execution.id)
            if item.purpose == "workload"
        )
        _assert_target_container(workload, repository)

        # The backend side effect happened but no tool result is settled.  A user
        # edits the same file before lease takeover; recovery must only stop the
        # old container and must never replay the command.
        target.write_text("# User edit\n", encoding="utf-8")
        provider = FakeProvider([])
        clock.now += timedelta(seconds=61)
        current = store.get_task(task.id)
        result = AgentRuntime(
            provider,
            _file_gateway(repository, interrupt=False),
            state_store=store,
            ownership_manager=_manager(store, clock, "execution-user-edit-2"),
            owner_id="worker-after-crash",
        ).resume(
            current,
            store.get_checkpoint(task.id),
        )
        second_execution = store.get_execution("execution-user-edit-2")
        second = second_execution
        assert result.runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED
        assert provider.requests == []
    finally:
        if worker.is_alive():
            try:
                sandbox.terminate_all("acceptance-finally")
            except BaseException as exc:
                cleanup_errors.append(exc)
        worker.join(timeout=10)

    assert second is not None
    assert not worker.is_alive()
    assert cleanup_errors == []
    assert errors == []
    assert target.read_text(encoding="utf-8") == "# User edit\n"
    recovered = next(
        item
        for item in store.list_managed_commands(first.execution.id)
        if item.purpose == "workload"
    )
    assert recovered.status in {ManagedCommandStatus.EXITED, ManagedCommandStatus.TERMINATED}
    assert _inspect_docker_container(recovered.container_id or "") is None
    reconciled = store.get_effect(interrupted.id)
    assert reconciled.status.value == "unknown"
    assert reconciled.reconciliation_evidence["source"] == "file_state_unconfirmed"
    assert store.get_tool_result(task.id, "docker-file-effect") is None
