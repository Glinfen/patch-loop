"""Detached Docker lifecycle supervisor used by :mod:`patchloop.sandbox`."""

from __future__ import annotations

import json
import os
import queue
import selectors
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from patchloop.sandbox import (
    ManagedCommandIdentity,
    SandboxCleanupError,
    _docker_environment,
    _inspect_docker_container,
    _remove_verified_container,
    _run_docker_control,
)

PROTOCOL_VERSION = 1
MAX_CONTROL_BYTES = 64 * 1024
MAX_FILE_BYTES = 16 * 1024
MAX_DIAGNOSTIC_CHARS = 8192


class SupervisorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: int = PROTOCOL_VERSION
    command_id: str
    execution_id: str
    container_name: str
    docker_host: str | None = None
    purpose: Literal["workload", "preflight"] = "workload"
    create_command: list[str] = Field(min_length=2)
    control_dir: str


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError("supervisor control file exceeds size limit")
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("xb") as stream:
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _read_initial_request() -> SupervisorRequest:
    raw = sys.stdin.buffer.readline(MAX_CONTROL_BYTES + 1)
    if not raw or len(raw) > MAX_CONTROL_BYTES or not raw.endswith(b"\n"):
        raise ValueError("invalid supervisor initialization message")
    request = SupervisorRequest.model_validate_json(raw)
    if request.protocol_version != PROTOCOL_VERSION:
        raise ValueError("unsupported supervisor protocol version")
    control_dir = Path(request.control_dir).resolve(strict=True)
    if not control_dir.is_dir():
        raise ValueError("supervisor control directory is invalid")
    return request


def _control_reader(events: queue.Queue[str], stopped: threading.Event) -> None:
    selector: selectors.BaseSelector | None = None
    if os.name != "nt":
        selector = selectors.DefaultSelector()
        selector.register(sys.stdin.buffer, selectors.EVENT_READ)
    try:
        while not stopped.is_set():
            if selector is not None and not selector.select(timeout=0.1):
                continue
            raw = sys.stdin.buffer.readline(128)
            if not raw:
                events.put("EOF")
                return
            try:
                message = raw.decode("ascii").strip()
            except UnicodeDecodeError:
                events.put("INVALID")
                return
            if message not in {"START", "STOP"}:
                events.put("INVALID")
                return
            events.put(message)
    finally:
        if selector is not None:
            selector.close()


@dataclass
class _Outcome:
    status: str
    exit_code: int | None
    reason: str | None
    oom_killed: bool
    cleanup_confirmed: bool
    diagnostic: str = ""


class Supervisor:
    def __init__(self, request: SupervisorRequest) -> None:
        self.request = request
        self.control_dir = Path(request.control_dir).resolve(strict=True)
        self.identity: ManagedCommandIdentity | None = None
        self.parent_gone = False

    def run(self) -> int:
        outcome: _Outcome
        try:
            identity = self._create_stopped_container()
            self.identity = identity
            _atomic_json(
                self.control_dir / "ready.json",
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "command_id": identity.id,
                    "execution_id": identity.execution_id,
                    "container_id": identity.container_id,
                    "container_name": identity.container_name,
                    "docker_host": identity.docker_host,
                    "stage": "ready",
                },
            )
            outcome = self._wait_and_start(identity)
        except BaseException as exc:
            outcome = self._cleanup_after_failure(exc)
        if os.name != "nt" and os.getppid() == 1:
            self.parent_gone = True
        self._write_outcome(outcome)
        if self.parent_gone:
            sleep(0.1)
            shutil.rmtree(self.control_dir, ignore_errors=True)
        return 0 if outcome.cleanup_confirmed else 3

    def _create_stopped_container(self) -> ManagedCommandIdentity:
        created = _run_docker_control(
            self.request.create_command,
            timeout_seconds=15,
            docker_host=self.request.docker_host,
        )
        if created.returncode != 0:
            raise SandboxCleanupError(created.stderr.strip() or "docker create failed")
        container_id = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
        if not container_id:
            raise SandboxCleanupError("docker create did not return a container ID")
        snapshot = _inspect_docker_container(container_id, docker_host=self.request.docker_host)
        if snapshot is None:
            raise SandboxCleanupError("created container was not inspectable")
        if (
            snapshot.command_id != self.request.command_id
            or snapshot.execution_id != self.request.execution_id
        ):
            raise SandboxCleanupError("created container identity labels do not match")
        return ManagedCommandIdentity(
            id=self.request.command_id,
            execution_id=self.request.execution_id,
            backend="docker",
            process_id=os.getpid(),
            process_start_marker="supervisor-pending-parent-marker",
            container_name=self.request.container_name,
            container_id=snapshot.container_id,
            docker_host=self.request.docker_host,
            purpose=self.request.purpose,
        )

    def _wait_and_start(self, identity: ManagedCommandIdentity) -> _Outcome:
        events: queue.Queue[str] = queue.Queue(maxsize=4)
        reader_stopped = threading.Event()
        reader = threading.Thread(
            target=_control_reader,
            args=(events, reader_stopped),
            name=f"sandbox-control-{identity.id}",
            daemon=True,
        )
        reader.start()
        try:
            event = events.get()
            if event != "START":
                self.parent_gone = event == "EOF"
                reason = "parent_eof" if self.parent_gone else "stopped_before_start"
                self._cleanup_with_retry(identity)
                return _Outcome("terminated", None, reason, False, True)

            start_process = subprocess.Popen(
                ["docker", "start", "--attach", identity.container_id or ""],
                env=_docker_environment(identity.docker_host),
                stdin=subprocess.DEVNULL,
                stdout=sys.stdout.buffer,
                stderr=sys.stderr.buffer,
                shell=False,
            )
            requested_stop: str | None = None
            while start_process.poll() is None:
                try:
                    event = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if event in {"STOP", "EOF", "INVALID"}:
                    self.parent_gone = event == "EOF"
                    requested_stop = "parent_eof" if self.parent_gone else "stop_requested"
                    self._cleanup_with_retry(identity)
                    break
            try:
                start_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                start_process.kill()
                start_process.wait(timeout=5)
            if requested_stop is None:
                try:
                    trailing_event = events.get(timeout=0.05)
                except queue.Empty:
                    trailing_event = None
                if trailing_event == "EOF":
                    self.parent_gone = True
                elif trailing_event in {"STOP", "INVALID"}:
                    requested_stop = "stop_requested"

            snapshot = _inspect_docker_container(
                identity.container_id or "", docker_host=identity.docker_host
            )
            exit_code: int | None = None
            oom_killed = False
            state_error = ""
            started = False
            if snapshot is not None:
                raw_exit = snapshot.state.get("ExitCode")
                exit_code = raw_exit if isinstance(raw_exit, int) else None
                oom_killed = snapshot.state.get("OOMKilled") is True
                raw_error = snapshot.state.get("Error")
                state_error = raw_error if isinstance(raw_error, str) else ""
                raw_started = snapshot.state.get("StartedAt")
                started = (
                    isinstance(raw_started, str)
                    and bool(raw_started)
                    and not raw_started.startswith("0001-01-01")
                )
                self._cleanup_with_retry(identity)
            elif requested_stop is None:
                # A lease-takeover reconciler may remove this exact, labelled
                # container while the old supervisor is still attached to it.
                # A successful absent result is authoritative cleanup evidence;
                # transport failures and identity mismatches have already raised
                # from _inspect_docker_container instead of returning None.
                return _Outcome(
                    "terminated",
                    None,
                    "container_removed_externally",
                    False,
                    True,
                )
            if requested_stop is not None:
                return _Outcome("terminated", exit_code, requested_stop, oom_killed, True)
            if start_process.returncode != 0 and not started:
                return _Outcome(
                    "cleanup_failed",
                    None,
                    "docker_start_failed",
                    False,
                    True,
                    (state_error or f"docker start exited {start_process.returncode}")[:8192],
                )
            return _Outcome("exited", exit_code, state_error or None, oom_killed, True)
        finally:
            reader_stopped.set()
            reader.join(timeout=1)

    def _cleanup_with_retry(self, identity: ManagedCommandIdentity) -> None:
        deadline = monotonic() + 20
        last_error: BaseException | None = None
        while monotonic() < deadline:
            try:
                _remove_verified_container(identity)
                return
            except BaseException as exc:
                last_error = exc
                sleep(min(5, max(0.0, deadline - monotonic())))
        raise SandboxCleanupError("supervisor cleanup deadline expired") from last_error

    def _cleanup_after_failure(self, failure: BaseException) -> _Outcome:
        diagnostic = f"{type(failure).__name__}: {failure}"[:MAX_DIAGNOSTIC_CHARS]
        identity = self.identity
        absence_confirmed = False
        if identity is None:
            try:
                snapshot = _inspect_docker_container(
                    self.request.container_name,
                    docker_host=self.request.docker_host,
                )
                if snapshot is not None:
                    identity = ManagedCommandIdentity(
                        id=self.request.command_id,
                        execution_id=self.request.execution_id,
                        backend="docker",
                        process_id=os.getpid(),
                        process_start_marker="supervisor-failure",
                        container_name=self.request.container_name,
                        container_id=snapshot.container_id,
                        docker_host=self.request.docker_host,
                        purpose=self.request.purpose,
                    )
                else:
                    absence_confirmed = True
            except BaseException as inspect_error:
                diagnostic = (
                    f"{diagnostic}; inspect: {type(inspect_error).__name__}: {inspect_error}"
                )[:MAX_DIAGNOSTIC_CHARS]
        if identity is None:
            return _Outcome(
                "cleanup_failed",
                None,
                "create_failed",
                False,
                absence_confirmed,
                diagnostic,
            )
        try:
            self._cleanup_with_retry(identity)
        except BaseException as cleanup_error:
            diagnostic = (
                f"{diagnostic}; cleanup: {type(cleanup_error).__name__}: {cleanup_error}"
            )[:MAX_DIAGNOSTIC_CHARS]
            return _Outcome("cleanup_failed", None, "cleanup_failed", False, False, diagnostic)
        return _Outcome("cleanup_failed", None, "startup_failed", False, True, diagnostic)

    def _write_outcome(self, outcome: _Outcome) -> None:
        identity = self.identity
        _atomic_json(
            self.control_dir / "outcome.json",
            {
                "protocol_version": PROTOCOL_VERSION,
                "command_id": self.request.command_id,
                "execution_id": self.request.execution_id,
                "container_id": None if identity is None else identity.container_id,
                "status": outcome.status,
                "exit_code": outcome.exit_code,
                "reason": outcome.reason,
                "oom_killed": outcome.oom_killed,
                "cleanup_confirmed": outcome.cleanup_confirmed,
                "diagnostic": outcome.diagnostic[:MAX_DIAGNOSTIC_CHARS],
            },
        )


def main() -> int:
    try:
        request = _read_initial_request()
        return Supervisor(request).run()
    except BaseException:
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
