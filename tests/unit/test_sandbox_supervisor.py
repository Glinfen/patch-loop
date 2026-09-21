from __future__ import annotations

import sys
from pathlib import Path

import pytest

from patchloop.sandbox import (
    DockerSandbox,
    ManagedCommandIdentity,
    ManagedCommandStatus,
    SandboxTimeoutError,
)

_FAKE_SUPERVISOR = r'''
import json
import pathlib
import sys

request = json.loads(sys.stdin.buffer.readline())
control = pathlib.Path(request["control_dir"])
container_id = "a" * 64
ready = {
    "protocol_version": 1,
    "command_id": request["command_id"],
    "execution_id": request["execution_id"],
    "container_id": container_id,
    "container_name": request["container_name"],
    "docker_host": request["docker_host"],
    "stage": "ready",
}
(control / "ready.json").write_text(json.dumps(ready), encoding="utf-8")
message = sys.stdin.buffer.readline().decode().strip()
command = request["create_command"]
mode = command[-1]
status = "terminated"
exit_code = None
reason = "stop_requested"
cleanup_confirmed = True
if message == "START":
    if len(command) >= 2 and command[-2] == "marker":
        pathlib.Path(command[-1]).write_text("started", encoding="utf-8")
        mode = "normal"
    if mode == "sleep":
        message = sys.stdin.buffer.readline().decode().strip()
        status = "terminated"
        reason = "stop_requested"
    else:
        print("supervised-output", flush=True)
        status = "exited"
        exit_code = 125 if mode == "125" else 0
        reason = None
elif message != "STOP":
    reason = "parent_eof"
outcome = {
    "protocol_version": 1,
    "command_id": request["command_id"],
    "execution_id": request["execution_id"],
    "container_id": container_id,
    "status": status,
    "exit_code": exit_code,
    "reason": reason,
    "oom_killed": False,
    "cleanup_confirmed": cleanup_confirmed,
    "diagnostic": "",
}
(control / "outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
'''


def _sandbox(tmp_path: Path) -> DockerSandbox:
    script = tmp_path / "fake_supervisor.py"
    script.write_text(_FAKE_SUPERVISOR, encoding="utf-8")
    return DockerSandbox(_supervisor_command=[sys.executable, str(script)])


def test_supervisor_does_not_start_workload_before_registration(tmp_path: Path) -> None:
    marker = tmp_path / "started.txt"
    started: list[ManagedCommandIdentity] = []
    finished: list[ManagedCommandIdentity] = []
    sandbox = _sandbox(tmp_path)

    def registered(identity: ManagedCommandIdentity) -> None:
        assert not marker.exists()
        assert identity.container_id == "a" * 64
        started.append(identity)

    sandbox.bind_execution(
        "execution-1",
        command_started=registered,
        command_finished=finished.append,
        interruption_probe=lambda: None,
    )

    result = sandbox._execute_managed(
        ["marker", str(marker)],
        tmp_path,
        timeout_seconds=5,
        max_output_chars=1000,
        purpose="workload",
    )
    sandbox.unbind_execution()

    assert marker.read_text(encoding="utf-8") == "started"
    assert result.exit_code == 0
    assert result.output.strip() == "supervised-output"
    assert len(started) == len(finished) == 1
    assert finished[0].status is ManagedCommandStatus.EXITED


def test_registration_failure_sends_stop_before_start(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-start.txt"
    sandbox = _sandbox(tmp_path)
    sandbox.bind_execution(
        "execution-1",
        command_started=lambda identity: (_ for _ in ()).throw(RuntimeError("lease lost")),
        command_finished=lambda identity: None,
        interruption_probe=lambda: None,
    )

    with pytest.raises(RuntimeError, match="lease lost"):
        sandbox._execute_managed(
            ["marker", str(marker)],
            tmp_path,
            timeout_seconds=5,
            max_output_chars=1000,
            purpose="workload",
        )

    assert not marker.exists()
    sandbox.unbind_execution()


def test_user_exit_125_is_not_misclassified_as_docker_start_failure(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)

    result = sandbox._execute_managed(
        ["fake", "125"],
        tmp_path,
        timeout_seconds=5,
        max_output_chars=1000,
        purpose="workload",
    )

    assert result.exit_code == 125


def test_supervisor_timeout_finishes_once(tmp_path: Path) -> None:
    finished: list[ManagedCommandIdentity] = []
    sandbox = _sandbox(tmp_path)
    sandbox.bind_execution(
        "execution-1",
        command_started=lambda identity: None,
        command_finished=finished.append,
        interruption_probe=lambda: None,
    )

    with pytest.raises(SandboxTimeoutError):
        sandbox._execute_managed(
            ["fake", "sleep"],
            tmp_path,
            timeout_seconds=0.2,
            max_output_chars=1000,
            purpose="workload",
        )
    sandbox.terminate_all("duplicate")
    sandbox.unbind_execution()

    assert len(finished) == 1
    assert finished[0].status is ManagedCommandStatus.TERMINATED
