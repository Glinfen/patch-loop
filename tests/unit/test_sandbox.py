import subprocess
import sys
import time
from pathlib import Path

import pytest

import patchloop.sandbox as sandbox_module
from patchloop.sandbox import (
    DockerSandbox,
    DockerSandboxConfig,
    LocalProcessSandbox,
    ManagedCommandIdentity,
    ManagedCommandStatus,
    SandboxCleanupError,
    SandboxInterruptedError,
    SandboxTimeoutError,
    _inspect_docker_container,
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
        sandbox_module,
        "_run_docker_control",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, "", "error: no such object: patchloop-command-1"
        ),
    )

    recovered = reconcile_managed_command(identity)

    assert recovered.status is ManagedCommandStatus.TERMINATED
    assert recovered.cleanup_reason == "recovery_already_stopped"

    monkeypatch.setattr(
        sandbox_module,
        "_run_docker_control",
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
            subprocess.CompletedProcess(
                [],
                0,
                '[{"Id":"container-id-1234567890","Name":"/patchloop-command-1",'
                '"Config":{"Labels":{"patchloop.command_id":"command-1",'
                '"patchloop.execution_id":"execution-1"}},"State":{"Running":true}}]',
                "",
            ),
            subprocess.CompletedProcess([], 0, "patchloop-command-1\n", ""),
            subprocess.CompletedProcess(
                [], 1, "", "Error: No such object: container-id-1234567890"
            ),
        ]
    )
    monkeypatch.setattr(
        sandbox_module, "_run_docker_control", lambda *args, **kwargs: next(responses)
    )

    recovered = reconcile_managed_command(identity)

    assert recovered.status is ManagedCommandStatus.TERMINATED
    assert recovered.cleanup_reason == "recovery_terminated"


@pytest.mark.parametrize(
    ("stderr", "match"),
    [
        ("permission denied", "permission denied"),
        ("Cannot connect to the Docker daemon", "Docker daemon"),
        ("Error: No such object: some-other-container", "No such object"),
    ],
)
def test_docker_inspect_does_not_treat_unknown_failures_as_absent(
    monkeypatch: pytest.MonkeyPatch, stderr: str, match: str
) -> None:
    monkeypatch.setattr(
        sandbox_module,
        "_run_docker_control",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", stderr),
    )

    with pytest.raises(SandboxCleanupError, match=match):
        _inspect_docker_container("patchloop-command-1")


def test_docker_recovery_rejects_label_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = ManagedCommandIdentity(
        id="command-1",
        execution_id="execution-1",
        backend="docker",
        process_id=1,
        process_start_marker="docker-client",
        container_name="patchloop-command-1",
    )
    monkeypatch.setattr(
        sandbox_module,
        "_run_docker_control",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            '[{"Id":"container-id-1234567890","Name":"/patchloop-command-1",'
            '"Config":{"Labels":{"patchloop.command_id":"other",'
            '"patchloop.execution_id":"execution-1"}},"State":{}}]',
            "",
        ),
    )

    with pytest.raises(SandboxCleanupError, match="identity changed"):
        reconcile_managed_command(identity)


def test_docker_inspect_rejects_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_module,
        "_run_docker_control",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "not-json", ""),
    )

    with pytest.raises(SandboxCleanupError, match="malformed JSON"):
        _inspect_docker_container("patchloop-command-1")


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


def test_failed_outcome_callback_is_retried_without_regressing_status(tmp_path: Path) -> None:
    attempts: list[ManagedCommandIdentity] = []

    def finish(identity: ManagedCommandIdentity) -> None:
        attempts.append(identity)
        if len(attempts) == 1:
            raise RuntimeError("temporary database failure")

    sandbox = LocalProcessSandbox()
    sandbox.bind_execution(
        "execution-1",
        command_started=lambda identity: None,
        command_finished=finish,
        interruption_probe=lambda: None,
    )

    with pytest.raises(SandboxCleanupError, match="persist managed command outcome"):
        sandbox.execute(
            [sys.executable, "-c", "print('finished')"],
            tmp_path,
            timeout_seconds=5,
            max_output_chars=1_000,
        )
    sandbox.terminate_all("late cleanup")
    sandbox.unbind_execution()

    assert len(attempts) == 2
    assert attempts[0].status is attempts[1].status is ManagedCommandStatus.EXITED
