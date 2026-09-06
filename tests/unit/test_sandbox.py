import subprocess
import sys
import time
from pathlib import Path

import pytest

from patchloop.sandbox import (
    DockerSandbox,
    DockerSandboxConfig,
    LocalProcessSandbox,
    ManagedCommandIdentity,
    ManagedCommandStatus,
    SandboxCleanupError,
    SandboxInterruptedError,
    SandboxTimeoutError,
    reconcile_managed_command,
)


def test_docker_sandbox_command_is_networkless_and_resource_bounded(tmp_path: Path) -> None:
    sandbox = DockerSandbox(
        DockerSandboxConfig(
            image="patchloop-sandbox:py313",
            cpus=0.5,
            memory_mb=256,
            pids_limit=64,
        )
    )

    command = sandbox.build_command(["python", "-m", "pytest", "-q"], tmp_path)

    assert command[:3] == ["docker", "run", "--rm"]
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--cpus") + 1] == "0.5"
    assert command[command.index("--memory") + 1] == "256m"
    assert command[command.index("--pids-limit") + 1] == "64"
    assert "--read-only" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    mount = command[command.index("--mount") + 1]
    assert f"source={tmp_path.resolve()}" in mount
    assert "target=/workspace" in mount
    assert command[-4:] == ["python", "-m", "pytest", "-q"]


def test_network_can_only_be_enabled_explicitly(tmp_path: Path) -> None:
    sandbox = DockerSandbox(DockerSandboxConfig(network_enabled=True))

    command = sandbox.build_command(["git", "status", "--short"], tmp_path)

    assert command[command.index("--network") + 1] == "bridge"


def test_managed_docker_command_has_verifiable_identity_labels(tmp_path: Path) -> None:
    sandbox = DockerSandbox()

    command, container_name = sandbox.build_managed_command(
        ["python", "-m", "pytest", "-q"],
        tmp_path,
        command_id="command-1",
        execution_id="execution-1",
    )

    assert container_name == "patchloop-command-1"
    assert command[command.index("--name") + 1] == container_name
    assert "patchloop.command_id=command-1" in command
    assert "patchloop.execution_id=execution-1" in command


def test_docker_recovery_distinguishes_absent_container_from_daemon_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ManagedCommandIdentity(
        id="command-1",
        execution_id="execution-1",
        backend="docker",
        process_id=1,
        process_start_marker="docker-client",
        container_name="patchloop-command-1",
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, "", "Error: No such object: patchloop-command-1"
        ),
    )

    recovered = reconcile_managed_command(identity)

    assert recovered.status is ManagedCommandStatus.TERMINATED
    assert recovered.cleanup_reason == "recovery_already_stopped"

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, "", "Cannot connect to the Docker daemon"
        ),
    )
    with pytest.raises(SandboxCleanupError, match="Docker daemon"):
        reconcile_managed_command(identity)


def test_docker_recovery_verifies_label_removal_and_final_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ManagedCommandIdentity(
        id="command-1",
        execution_id="execution-1",
        backend="docker",
        process_id=1,
        process_start_marker="docker-client",
        container_name="patchloop-command-1",
    )
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, "command-1\n", ""),
            subprocess.CompletedProcess([], 0, "patchloop-command-1\n", ""),
            subprocess.CompletedProcess([], 1, "", "Error: No such object: patchloop-command-1"),
        ]
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: next(responses))

    recovered = reconcile_managed_command(identity)

    assert recovered.status is ManagedCommandStatus.TERMINATED
    assert recovered.cleanup_reason == "recovery_terminated"


def test_local_sandbox_records_verifiable_process_lifecycle(tmp_path: Path) -> None:
    started = []
    finished = []
    sandbox = LocalProcessSandbox()
    sandbox.bind_execution(
        "execution-1",
        command_started=started.append,
        command_finished=finished.append,
        interruption_probe=lambda: None,
    )

    result = sandbox.execute(
        [sys.executable, "-c", "print('managed')"],
        tmp_path,
        timeout_seconds=5,
        max_output_chars=1_000,
    )
    sandbox.unbind_execution()

    assert result.exit_code == 0
    assert result.output.strip() == "managed"
    assert len(started) == len(finished) == 1
    assert started[0].execution_id == "execution-1"
    assert started[0].process_start_marker
    assert finished[0].status is ManagedCommandStatus.EXITED


def test_local_timeout_terminates_the_child_process_tree(tmp_path: Path) -> None:
    child_marker = tmp_path / "child-survived.txt"
    child_code = (
        "import pathlib,time; time.sleep(1); "
        f"pathlib.Path({str(child_marker)!r}).write_text('alive')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(10)"
    )
    finished = []
    sandbox = LocalProcessSandbox()
    sandbox.bind_execution(
        "execution-1",
        command_started=lambda identity: None,
        command_finished=finished.append,
        interruption_probe=lambda: None,
    )

    with pytest.raises(SandboxTimeoutError):
        sandbox.execute(
            [sys.executable, "-c", parent_code],
            tmp_path,
            timeout_seconds=0.3,
            max_output_chars=1_000,
        )
    sandbox.unbind_execution()
    time.sleep(1.1)

    assert not child_marker.exists()
    assert finished[0].status is ManagedCommandStatus.TERMINATED
    assert finished[0].cleanup_reason == "timeout"


def test_local_sandbox_interrupts_for_runtime_control(tmp_path: Path) -> None:
    finished = []
    sandbox = LocalProcessSandbox()
    sandbox.bind_execution(
        "execution-1",
        command_started=lambda identity: None,
        command_finished=finished.append,
        interruption_probe=lambda: "pause",
    )

    with pytest.raises(SandboxInterruptedError, match="pause"):
        sandbox.execute(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            tmp_path,
            timeout_seconds=5,
            max_output_chars=1_000,
        )
    sandbox.unbind_execution()

    assert finished[0].status is ManagedCommandStatus.TERMINATED
    assert finished[0].cleanup_reason == "pause"
