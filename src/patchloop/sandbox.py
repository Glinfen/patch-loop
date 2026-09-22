"""Command execution backends with a fail-closed Docker sandbox."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import monotonic, sleep
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from patchloop.sandbox_output import (
    CapturedOutput,
    OutputCollectionError,
    OutputCollectionInterrupted,
    OutputCollectionTimeout,
    collect_process_output,
)
from patchloop.sandbox_storage import (
    WorkspaceCapacityError,
    WorkspaceCapacityEvidence,
    same_workspace_capacity,
    verify_workspace_capacity,
)

if TYPE_CHECKING:
    from patchloop.domain import TaskExecutionConfig


class SandboxError(RuntimeError):
    pass


class SandboxTimeoutError(TimeoutError):
    pass


class SandboxCleanupError(RuntimeError):
    pass


class SandboxInterruptedError(SandboxError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"managed command interrupted: {reason}")
        self.reason = reason


class ManagedCommandStatus(StrEnum):
    RUNNING = "running"
    EXITED = "exited"
    TERMINATED = "terminated"
    CLEANUP_FAILED = "cleanup_failed"


def _validate_local_docker_host(value: str | None) -> str | None:
    if value is None:
        return None
    socket_path = value.removeprefix("unix://")
    if not value.startswith("unix:///") or not socket_path.startswith("/"):
        raise ValueError("docker_host must be an absolute local unix socket URL")
    return value


class ManagedCommandIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    execution_id: str = Field(min_length=1, max_length=128)
    backend: str = Field(min_length=1, max_length=32)
    process_id: int = Field(gt=0)
    process_start_marker: str = Field(min_length=1, max_length=256)
    container_name: str | None = Field(default=None, min_length=1, max_length=128)
    container_id: str | None = Field(default=None, min_length=12, max_length=128)
    docker_host: str | None = Field(default=None, max_length=4096)
    purpose: Literal["workload", "preflight"] = "workload"
    status: ManagedCommandStatus = ManagedCommandStatus.RUNNING
    cleanup_reason: str | None = Field(default=None, max_length=256)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("docker_host")
    @classmethod
    def _validate_docker_host(cls, value: str | None) -> str | None:
        return _validate_local_docker_host(value)


CommandStarted = Callable[[ManagedCommandIdentity], None]
CommandFinished = Callable[[ManagedCommandIdentity], None]
InterruptionProbe = Callable[[], str | None]


class SandboxResult(BaseModel):
    exit_code: int
    output: str
    backend: str
    output_truncated: bool = False
    stdout_bytes: int = Field(default=0, ge=0)
    stderr_bytes: int = Field(default=0, ge=0)
    oom_killed: bool = False


class CommandSandbox(Protocol):
    name: str

    def execute(
        self,
        command: list[str],
        repository: Path,
        *,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> SandboxResult: ...


@runtime_checkable
class ManagedCommandSandbox(Protocol):
    def bind_execution(
        self,
        execution_id: str,
        *,
        command_started: CommandStarted,
        command_finished: CommandFinished,
        interruption_probe: InterruptionProbe,
    ) -> None: ...

    def terminate_all(self, reason: str) -> None: ...

    def unbind_execution(self) -> None: ...


def _minimal_environment() -> dict[str, str]:
    environment = {
        key: value
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if (value := os.environ.get(key)) is not None
    }
    environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
    return environment


def _windows_kernel32() -> Any:
    import ctypes

    return ctypes.__dict__["windll"].kernel32


def _process_start_marker(process: subprocess.Popen[Any]) -> str:
    """Return an OS-issued creation marker so cleanup never trusts a PID alone."""

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        kernel32 = _windows_kernel32()
        if not kernel32.GetProcessTimes(
            wintypes.HANDLE(int(process._handle)),  # type: ignore[attr-defined]
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise SandboxCleanupError("could not read the process creation marker")
        value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return f"windows-filetime:{value}"
    stat_path = Path(f"/proc/{process.pid}/stat")
    try:
        fields = stat_path.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return f"linux-start-ticks:{fields[19]}"
    except (OSError, IndexError) as exc:
        raise SandboxCleanupError("could not read the process creation marker") from exc


def _windows_process_start_marker(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = _windows_kernel32()
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)
    if not handle:
        error = kernel32.GetLastError()
        if error == 87:
            return None
        raise SandboxCleanupError(f"could not inspect managed process {pid}: {error}")
    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise SandboxCleanupError(f"could not inspect managed process {pid}")
        if exit_code.value != 259:
            return None
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise SandboxCleanupError(f"could not inspect managed process {pid}")
    finally:
        kernel32.CloseHandle(handle)
    value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    return f"windows-filetime:{value}"


def _local_process_start_marker(pid: int) -> str | None:
    if os.name == "nt":
        return _windows_process_start_marker(pid)
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        fields = stat_path.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None
    except (OSError, IndexError) as exc:
        raise SandboxCleanupError(f"could not inspect managed process {pid}") from exc
    return f"linux-start-ticks:{fields[19]}"


def _wait_for_local_identity_exit(pid: int, marker: str, timeout_seconds: float) -> bool:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if _local_process_start_marker(pid) != marker:
            return True
        sleep(0.05)
    return _local_process_start_marker(pid) != marker


def _windows_descendant_pids(root_pid: int) -> list[int]:
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = _windows_kernel32()
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise SandboxCleanupError("could not enumerate the process tree")
    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    parents: dict[int, list[int]] = {}
    try:
        success = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while success:
            parents.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
            success = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    descendants: list[int] = []
    pending = list(parents.get(root_pid, ()))
    while pending:
        child = pending.pop()
        descendants.append(child)
        pending.extend(parents.get(child, ()))
    return descendants


def _terminate_windows_pid(pid: int) -> None:
    import ctypes

    kernel32 = _windows_kernel32()
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x0001 | 0x00100000, False, pid)
    if not handle:
        error = kernel32.GetLastError()
        if error == 87:
            return
        raise SandboxCleanupError(f"could not open child process {pid}: {error}")
    try:
        if not kernel32.TerminateProcess(handle, 1):
            error = kernel32.GetLastError()
            raise SandboxCleanupError(f"could not terminate child process {pid}: {error}")
        kernel32.WaitForSingleObject(handle, 5_000)
    finally:
        kernel32.CloseHandle(handle)


class DockerContainerSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    container_id: str
    name: str
    command_id: str | None = None
    execution_id: str | None = None
    state: dict[str, object] = Field(default_factory=dict)


def _docker_environment(docker_host: str | None) -> dict[str, str]:
    environment = _minimal_environment()
    if docker_host is not None:
        environment["DOCKER_HOST"] = docker_host
    return environment


def _run_docker_control(
    command: list[str], *, timeout_seconds: float, docker_host: str | None
) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            command,
            env=_docker_environment(docker_host),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise SandboxCleanupError("Docker is required for managed container cleanup") from exc

    def terminate(_: str) -> None:
        process.kill()
        process.wait(timeout=2)

    try:
        captured = collect_process_output(
            process,
            max_output_chars=8192,
            deadline=monotonic() + timeout_seconds,
            interruption_probe=None,
            terminate=terminate,
        )
    except OutputCollectionTimeout as exc:
        raise SandboxCleanupError(f"docker control command timed out: {command[1]}") from exc
    except OutputCollectionError as exc:
        raise SandboxCleanupError(f"docker control command output failed: {command[1]}") from exc
    stdout = captured.stdout_tail
    stderr = captured.stderr_tail
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _is_absent_docker_error(error: str, reference: str) -> bool:
    folded = error.casefold()
    absent = "no such object" in folded or "no such container" in folded
    return absent and reference.casefold() in folded


def _inspect_docker_container(
    reference: str, *, docker_host: str | None = None
) -> DockerContainerSnapshot | None:
    inspected = _run_docker_control(
        [
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            "{{json .Id}}\n{{json .Name}}\n{{json .Config.Labels}}\n"
            "{{json .State.Status}}\n{{json .State.Running}}\n"
            "{{json .State.ExitCode}}\n{{json .State.OOMKilled}}\n"
            "{{json .State.Error}}\n{{json .State.StartedAt}}",
            reference,
        ],
        timeout_seconds=5,
        docker_host=docker_host,
    )
    if inspected.returncode != 0:
        error = inspected.stderr.strip()
        if _is_absent_docker_error(error, reference):
            return None
        raise SandboxCleanupError(error or "docker inspect failed")
    try:
        output = inspected.stdout.strip()
        if output.startswith("["):
            payload = json.loads(output)
            if (
                not isinstance(payload, list)
                or len(payload) != 1
                or not isinstance(payload[0], dict)
            ):
                raise ValueError("inspect response must contain exactly one container")
            item = payload[0]
            container_id = item["Id"]
            name = item["Name"]
            config = item["Config"]
            state = item["State"]
            labels = config.get("Labels") or {} if isinstance(config, dict) else None
        else:
            lines = output.splitlines()
            if len(lines) != 9:
                raise ValueError("inspect response must contain nine JSON fields")
            values = [json.loads(line) for line in lines]
            container_id, name, labels = values[:3]
            state = {
                "Status": values[3],
                "Running": values[4],
                "ExitCode": values[5],
                "OOMKilled": values[6],
                "Error": values[7],
                "StartedAt": values[8],
            }
        if not isinstance(container_id, str) or not container_id:
            raise ValueError("container ID is missing")
        if not isinstance(name, str) or not name:
            raise ValueError("container name is missing")
        if labels is None:
            labels = {}
        if not isinstance(labels, dict) or not isinstance(state, dict):
            raise ValueError("container labels are malformed")
        command_id = labels.get("patchloop.command_id")
        execution_id = labels.get("patchloop.execution_id")
        if command_id is not None and not isinstance(command_id, str):
            raise ValueError("command identity label is malformed")
        if execution_id is not None and not isinstance(execution_id, str):
            raise ValueError("execution identity label is malformed")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SandboxCleanupError("docker inspect returned malformed JSON") from exc
    return DockerContainerSnapshot(
        container_id=container_id,
        name=name.removeprefix("/"),
        command_id=command_id,
        execution_id=execution_id,
        state=state,
    )


def _remove_verified_container(identity: ManagedCommandIdentity) -> bool:
    reference = identity.container_id or identity.container_name
    if reference is None:
        raise SandboxCleanupError("managed Docker command has no container identity")
    snapshot = _inspect_docker_container(reference, docker_host=identity.docker_host)
    if snapshot is None:
        return False
    if identity.container_id is not None and snapshot.container_id != identity.container_id:
        raise SandboxCleanupError("container ID changed before cleanup")
    if snapshot.command_id != identity.id or snapshot.execution_id != identity.execution_id:
        raise SandboxCleanupError("container identity changed before cleanup")
    removed = _run_docker_control(
        ["docker", "rm", "--force", snapshot.container_id],
        timeout_seconds=10,
        docker_host=identity.docker_host,
    )
    if removed.returncode != 0 and not _is_absent_docker_error(
        removed.stderr, snapshot.container_id
    ):
        raise SandboxCleanupError(removed.stderr.strip() or "docker rm failed")
    if (
        _inspect_docker_container(snapshot.container_id, docker_host=identity.docker_host)
        is not None
    ):
        raise SandboxCleanupError("managed container remained after recovery cleanup")
    return True


def _docker_command_id(container_name: str) -> str | None:
    """Compatibility helper retained for callers that only need the command label."""

    snapshot = _inspect_docker_container(container_name)
    return None if snapshot is None else snapshot.command_id


def _create_windows_kill_job(process: subprocess.Popen[Any]) -> int:
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = _windows_kernel32()
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise SandboxCleanupError("could not create a Windows process job")
    limits = ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = 0x00002000
    configured = kernel32.SetInformationJobObject(
        job,
        9,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    )
    assigned = configured and kernel32.AssignProcessToJobObject(
        job,
        wintypes.HANDLE(int(process._handle)),  # type: ignore[attr-defined]
    )
    if not assigned:
        kernel32.CloseHandle(job)
        raise SandboxCleanupError("could not assign the process to its cleanup job")
    return int(job)


def reconcile_managed_command(identity: ManagedCommandIdentity) -> ManagedCommandIdentity:
    """Confirm an orphaned command stopped, terminating only its verified identity."""

    reason = "recovery_already_stopped"
    if identity.backend == "local":
        marker = _local_process_start_marker(identity.process_id)
        if marker == identity.process_start_marker:
            if os.name == "nt":
                descendants = _windows_descendant_pids(identity.process_id)
                for child_pid in reversed(descendants):
                    _terminate_windows_pid(child_pid)
                _terminate_windows_pid(identity.process_id)
            else:
                killpg = os.__dict__["killpg"]
                with suppress(ProcessLookupError):
                    killpg(identity.process_id, signal.SIGTERM)
                if not _wait_for_local_identity_exit(
                    identity.process_id, identity.process_start_marker, 2
                ):
                    with suppress(ProcessLookupError):
                        killpg(identity.process_id, getattr(signal, "SIGKILL", 9))
            if not _wait_for_local_identity_exit(
                identity.process_id, identity.process_start_marker, 3
            ):
                raise SandboxCleanupError("managed process remained alive after recovery cleanup")
            reason = "recovery_terminated"
    elif identity.backend == "docker":
        if _remove_verified_container(identity):
            reason = "recovery_terminated"
    else:
        raise SandboxCleanupError(f"unsupported managed command backend: {identity.backend}")
    return identity.model_copy(
        update={
            "status": ManagedCommandStatus.TERMINATED,
            "cleanup_reason": reason,
            "updated_at": datetime.now(UTC),
        }
    )


class _ManagedSandboxBase:
    def __init__(self) -> None:
        self._execution_id: str | None = None
        self._command_started: CommandStarted | None = None
        self._command_finished: CommandFinished | None = None
        self._interruption_probe: InterruptionProbe | None = None
        self._active: dict[str, tuple[subprocess.Popen[bytes], ManagedCommandIdentity]] = {}
        self._active_lock = threading.Lock()
        self._command_locks: dict[str, threading.RLock] = {}
        self._pending_outcomes: dict[str, ManagedCommandIdentity] = {}
        self._settled_commands: set[str] = set()

    def bind_execution(
        self,
        execution_id: str,
        *,
        command_started: CommandStarted,
        command_finished: CommandFinished,
        interruption_probe: InterruptionProbe,
    ) -> None:
        with self._active_lock:
            if self._active:
                raise RuntimeError("cannot rebind a sandbox with active commands")
            self._execution_id = execution_id
            self._command_started = command_started
            self._command_finished = command_finished
            self._interruption_probe = interruption_probe

    def unbind_execution(self) -> None:
        with self._active_lock:
            if self._active:
                raise SandboxCleanupError("cannot unbind a sandbox with active commands")
            self._execution_id = None
            self._command_started = None
            self._command_finished = None
            self._interruption_probe = None

    def terminate_all(self, reason: str) -> None:
        with self._active_lock:
            active = list(self._active.values())
        failures: list[str] = []
        for process, identity in active:
            try:
                self._terminate_and_finish(process, identity, reason)
            except Exception as exc:
                failures.append(f"{identity.id}: {exc}")
        if failures:
            raise SandboxCleanupError("; ".join(failures))

    def _register(
        self,
        process: subprocess.Popen[bytes],
        *,
        backend: str,
        container_name: str | None = None,
        container_id: str | None = None,
        docker_host: str | None = None,
        purpose: Literal["workload", "preflight"] = "workload",
        identity_id: str | None = None,
        execution_id: str | None = None,
    ) -> ManagedCommandIdentity:
        identity = ManagedCommandIdentity(
            id=identity_id or str(uuid4()),
            execution_id=execution_id or self._execution_id or f"standalone-{uuid4()}",
            backend=backend,
            process_id=process.pid,
            process_start_marker=_process_start_marker(process),
            container_name=container_name,
            container_id=container_id,
            docker_host=docker_host,
            purpose=purpose,
        )
        with self._active_lock:
            self._active[identity.id] = (process, identity)
            self._command_locks.setdefault(identity.id, threading.RLock())
        try:
            if self._command_started is not None:
                self._command_started(identity)
        except BaseException:
            try:
                self._terminate_process(process, identity)
            except Exception as cleanup_error:
                raise SandboxCleanupError(
                    f"command registration and cleanup failed: {identity.id}"
                ) from cleanup_error
            else:
                with self._active_lock:
                    self._active.pop(identity.id, None)
            raise
        return identity

    def _finish(
        self,
        identity: ManagedCommandIdentity,
        status: ManagedCommandStatus,
        reason: str | None = None,
    ) -> None:
        with self._active_lock:
            command_lock = self._command_locks.setdefault(identity.id, threading.RLock())
        with command_lock:
            with self._active_lock:
                if identity.id in self._settled_commands:
                    return
                finished = self._pending_outcomes.get(identity.id)
                if finished is None:
                    finished = identity.model_copy(
                        update={
                            "status": status,
                            "cleanup_reason": reason,
                            "updated_at": datetime.now(UTC),
                        }
                    )
                    self._pending_outcomes[identity.id] = finished
            if self._command_finished is not None:
                try:
                    self._command_finished(finished)
                except Exception as exc:
                    raise SandboxCleanupError(
                        f"could not persist managed command outcome: {identity.id}"
                    ) from exc
            with self._active_lock:
                self._active.pop(identity.id, None)
                self._pending_outcomes.pop(identity.id, None)
                self._settled_commands.add(identity.id)

    def _terminate_and_finish(
        self,
        process: subprocess.Popen[bytes],
        identity: ManagedCommandIdentity,
        reason: str,
    ) -> None:
        with self._active_lock:
            command_lock = self._command_locks.setdefault(identity.id, threading.RLock())
        with command_lock:
            try:
                self._terminate_process(process, identity)
            except Exception as exc:
                try:
                    self._finish(identity, ManagedCommandStatus.CLEANUP_FAILED, str(exc))
                except Exception as finish_exc:
                    raise SandboxCleanupError(
                        f"command cleanup and outcome persistence failed: {identity.id}"
                    ) from finish_exc
                raise SandboxCleanupError(f"managed command cleanup failed: {identity.id}") from exc
            self._finish(identity, ManagedCommandStatus.TERMINATED, reason)

    def _communicate(
        self,
        process: subprocess.Popen[bytes],
        identity: ManagedCommandIdentity,
        timeout_seconds: float,
        max_output_chars: int,
        *,
        finish_on_exit: bool = True,
        interruption_probe: InterruptionProbe | None = None,
        readers_started: threading.Event | None = None,
    ) -> CapturedOutput:
        try:
            captured = collect_process_output(
                process,
                max_output_chars=max_output_chars,
                deadline=monotonic() + timeout_seconds,
                interruption_probe=(
                    self._interruption_probe if interruption_probe is None else interruption_probe
                ),
                terminate=lambda reason: self._terminate_and_finish(process, identity, reason),
                readers_started=readers_started,
            )
        except OutputCollectionTimeout:
            raise SandboxTimeoutError(
                f"{identity.backend} command timed out after {timeout_seconds:g} seconds"
            ) from None
        except OutputCollectionInterrupted as exc:
            raise SandboxInterruptedError(exc.reason) from None
        except OutputCollectionError as exc:
            raise SandboxCleanupError(
                f"managed command output cleanup failed: {identity.id}"
            ) from exc
        self._complete_process_scope(process, identity)
        if finish_on_exit:
            self._finish(identity, ManagedCommandStatus.EXITED)
        return captured

    def _terminate_process(
        self, process: subprocess.Popen[bytes], identity: ManagedCommandIdentity
    ) -> None:
        raise NotImplementedError

    def _complete_process_scope(
        self, process: subprocess.Popen[bytes], identity: ManagedCommandIdentity
    ) -> None:
        del process, identity


class LocalProcessSandbox(_ManagedSandboxBase):
    """Compatibility backend restricted by the command policy but not OS-isolated."""

    name = "local"

    def __init__(self) -> None:
        super().__init__()
        self._windows_jobs: dict[int, int] = {}

    def execute(
        self,
        command: list[str],
        repository: Path,
        *,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> SandboxResult:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=repository,
            env=_minimal_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
        if os.name == "nt":
            try:
                self._windows_jobs[process.pid] = _create_windows_kill_job(process)
            except Exception:
                process.kill()
                process.wait(timeout=5)
                raise
        identity = self._register(process, backend=self.name)
        captured = self._communicate(process, identity, timeout_seconds, max_output_chars)
        output = (captured.stdout_tail + captured.stderr_tail)[-max_output_chars:]
        return SandboxResult(
            exit_code=process.returncode,
            output=output,
            backend=self.name,
            output_truncated=captured.truncated,
            stdout_bytes=captured.stdout_bytes,
            stderr_bytes=captured.stderr_bytes,
        )

    def _terminate_process(
        self, process: subprocess.Popen[bytes], identity: ManagedCommandIdentity
    ) -> None:
        if process.poll() is not None:
            return
        if _process_start_marker(process) != identity.process_start_marker:
            raise SandboxCleanupError("process identity changed before cleanup")
        if os.name == "nt":
            job = self._windows_jobs.pop(process.pid, None)
            if job is not None:
                _windows_kernel32().CloseHandle(job)
            else:
                descendants = _windows_descendant_pids(process.pid)
                for child_pid in reversed(descendants):
                    _terminate_windows_pid(child_pid)
                process.kill()
        else:
            killpg = os.__dict__["killpg"]
            killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                killpg(process.pid, signal.__dict__["SIGKILL"])
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise SandboxCleanupError("process tree did not exit after termination") from exc

    def _complete_process_scope(
        self, process: subprocess.Popen[bytes], identity: ManagedCommandIdentity
    ) -> None:
        del identity
        if os.name == "nt":
            job = self._windows_jobs.pop(process.pid, None)
            if job is not None:
                _windows_kernel32().CloseHandle(job)


class DockerSandboxConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    image: str = Field(default="patchloop-sandbox:py313", pattern=r"^[A-Za-z0-9._/:@-]+$")
    cpus: float = Field(default=1.0, gt=0, le=8)
    memory_mb: int = Field(default=512, ge=64, le=16_384)
    pids_limit: int = Field(default=128, ge=16, le=4_096)
    network_enabled: bool = False
    user: str | None = Field(default=None, pattern=r"^\d+:\d+$")
    tmpfs_mb: int = Field(default=64, ge=16, le=1024)
    workspace_limit_mb: int | None = Field(default=None, ge=64, le=16_384)
    workspace_inode_limit: int = Field(default=65_536, ge=1024, le=10_000_000)


class _SupervisorReady(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: int
    command_id: str
    execution_id: str
    container_id: str
    container_name: str
    docker_host: str | None
    stage: Literal["ready"]


class _SupervisorOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: int
    command_id: str
    execution_id: str
    container_id: str | None
    status: Literal["exited", "terminated", "cleanup_failed"]
    exit_code: int | None
    reason: str | None
    oom_killed: bool
    cleanup_confirmed: bool
    diagnostic: str = Field(max_length=8192)


class _SupervisorHandle:
    def __init__(self, process: subprocess.Popen[bytes], control_dir: Path) -> None:
        self.process = process
        self.control_dir = control_dir
        self.lock = threading.Lock()
        self.stop_sent = False


class DockerSandbox(_ManagedSandboxBase):
    name = "docker"

    def __init__(
        self,
        config: DockerSandboxConfig | None = None,
        *,
        docker_host: str | None = None,
        _supervisor_command: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.config = config or DockerSandboxConfig()
        self.docker_host = _validate_local_docker_host(docker_host)
        self._supervisors: dict[str, _SupervisorHandle] = {}
        self._supervisor_command = _supervisor_command or [
            sys.executable,
            "-m",
            "patchloop.sandbox_supervisor",
        ]
        self._resolved_image_id: str | None = None
        self._preflight_cache: set[tuple[object, ...]] = set()
        self._workspace_capacity_evidence: WorkspaceCapacityEvidence | None = None

    def bind_execution(
        self,
        execution_id: str,
        *,
        command_started: CommandStarted,
        command_finished: CommandFinished,
        interruption_probe: InterruptionProbe,
    ) -> None:
        super().bind_execution(
            execution_id,
            command_started=command_started,
            command_finished=command_finished,
            interruption_probe=interruption_probe,
        )
        self._resolved_image_id = None
        self._preflight_cache.clear()
        self._workspace_capacity_evidence = None

    def unbind_execution(self) -> None:
        super().unbind_execution()
        self._resolved_image_id = None
        self._preflight_cache.clear()
        self._workspace_capacity_evidence = None

    def _effective_user(self) -> str | None:
        if self.config.user is not None:
            return self.config.user
        if os.name != "nt" and hasattr(os, "getuid") and hasattr(os, "getgid"):
            return f"{os.__dict__['getuid']()}:{os.__dict__['getgid']()}"
        return None

    @staticmethod
    def _container_command(command: list[str]) -> list[str]:
        result = list(command)
        executable = Path(result[0]).name.casefold()
        if executable in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}:
            result[0] = "python"
        return result

    def _sandbox_arguments(self, repository: Path) -> list[str]:
        repository = repository.resolve(strict=True)
        network = "bridge" if self.config.network_enabled else "none"
        arguments = [
            "--log-driver",
            "none",
            "--network",
            network,
            "--cpus",
            f"{self.config.cpus:g}",
            "--memory",
            f"{self.config.memory_mb}m",
            "--memory-swap",
            f"{self.config.memory_mb}m",
            "--pids-limit",
            str(self.config.pids_limit),
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={self.config.tmpfs_mb}m",
            "--mount",
            f"type=bind,source={repository},target=/workspace,bind-recursive=disabled",
            "--workdir",
            "/workspace",
        ]
        user = self._effective_user()
        if user is not None:
            arguments.extend(["--user", user])
        return arguments

    def build_command(self, command: list[str], repository: Path) -> list[str]:
        return [
            "docker",
            "run",
            "--rm",
            *self._sandbox_arguments(repository),
            self.config.image,
            *self._container_command(command),
        ]

    def build_create_command(
        self,
        command: list[str],
        repository: Path,
        *,
        command_id: str,
        execution_id: str,
        image: str | None = None,
        purpose: Literal["workload", "preflight"] = "workload",
    ) -> tuple[list[str], str]:
        container_name = f"patchloop-{command_id}"
        return (
            [
                "docker",
                "create",
                *self._sandbox_arguments(repository),
                "--name",
                container_name,
                "--label",
                f"patchloop.command_id={command_id}",
                "--label",
                f"patchloop.execution_id={execution_id}",
                "--label",
                f"patchloop.purpose={purpose}",
                image or self.config.image,
                *self._container_command(command),
            ],
            container_name,
        )

    def build_managed_command(
        self,
        command: list[str],
        repository: Path,
        *,
        command_id: str,
        execution_id: str,
    ) -> tuple[list[str], str]:
        container_name = f"patchloop-{command_id}"
        docker_command = self.build_command(command, repository)
        image_index = docker_command.index(self.config.image)
        docker_command[image_index:image_index] = [
            "--name",
            container_name,
            "--label",
            f"patchloop.command_id={command_id}",
            "--label",
            f"patchloop.execution_id={execution_id}",
        ]
        return docker_command, container_name

    def execute(
        self,
        command: list[str],
        repository: Path,
        *,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> SandboxResult:
        repository = repository.resolve(strict=True)
        capacity = self._verify_workspace_capacity(repository)
        image = self._resolve_image_id()
        self._ensure_preflight(repository, image)
        if capacity is not None:
            confirmed = self._verify_workspace_capacity(repository)
            if confirmed is None or not same_workspace_capacity(capacity, confirmed):
                raise SandboxError(
                    "workspace_capacity_unverified: mount identity changed after preflight"
                )
        return self._execute_managed(
            command,
            repository,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
            purpose="workload",
            image=image,
        )

    def _verify_workspace_capacity(self, repository: Path) -> WorkspaceCapacityEvidence | None:
        limit_mb = self.config.workspace_limit_mb
        if limit_mb is None:
            self._workspace_capacity_evidence = None
            return None
        try:
            evidence = verify_workspace_capacity(
                repository,
                limit_bytes=limit_mb * 1024 * 1024,
                inode_limit=self.config.workspace_inode_limit,
            )
        except WorkspaceCapacityError as exc:
            raise SandboxError(f"workspace_capacity_unverified: {exc}") from exc
        self._workspace_capacity_evidence = evidence
        return evidence

    def _resolve_image_id(self) -> str:
        if self._resolved_image_id is not None:
            return self._resolved_image_id
        inspected = _run_docker_control(
            ["docker", "image", "inspect", "--format", "{{.Id}}", self.config.image],
            timeout_seconds=10,
            docker_host=self.docker_host,
        )
        if inspected.returncode != 0:
            raise SandboxError(inspected.stderr.strip() or "Docker image is unavailable")
        image_id = inspected.stdout.strip()
        if not image_id.startswith("sha256:") or any(char.isspace() for char in image_id):
            raise SandboxError("Docker image inspect returned an invalid image ID")
        if self._execution_id is not None:
            self._resolved_image_id = image_id
        return image_id

    def _ensure_preflight(self, repository: Path, image: str) -> None:
        root_stat = repository.stat()
        user = self._effective_user()
        key = (
            self._execution_id,
            root_stat.st_dev,
            root_stat.st_ino,
            image,
            user,
            self.config.model_dump_json(),
        )
        if self._execution_id is not None and key in self._preflight_cache:
            return
        token = uuid4().hex
        input_path = repository / f".patchloop-preflight-input-{token}"
        output_path = repository / f".patchloop-preflight-output-{token}"
        try:
            with input_path.open("x", encoding="utf-8") as stream:
                stream.write(token)
            code = (
                "import json,os,pathlib;"
                f"token={token!r};"
                f"source=pathlib.Path('/workspace/{input_path.name}');"
                f"target=pathlib.Path('/workspace/{output_path.name}');"
                "content=source.read_text();"
                "fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);"
                "os.write(fd,content.encode());os.close(fd);"
                "read=lambda name:pathlib.Path('/sys/fs/cgroup',name).read_text().strip();"
                "print(json.dumps({'token':content,'uid':os.getuid(),'gid':os.getgid(),"
                "'memory_max':read('memory.max'),'memory_swap_max':read('memory.swap.max'),"
                "'pids_max':read('pids.max'),'cpu_max':read('cpu.max')}))"
            )
            result = self._execute_managed(
                ["python", "-c", code],
                repository,
                timeout_seconds=30,
                max_output_chars=8192,
                purpose="preflight",
                image=image,
            )
            if result.exit_code != 0:
                raise SandboxError(
                    f"workspace_identity_unverified: preflight exited {result.exit_code}: "
                    f"{result.output[-1000:]}"
                )
            evidence = self._last_json_object(result.output)
            self._validate_preflight_evidence(evidence, token, output_path, user)
        except SandboxError as exc:
            if str(exc).startswith("workspace_identity_unverified"):
                raise SandboxError(
                    f"{exc}; {self._preflight_identity_diagnostic(repository, image)}"
                ) from exc
            raise
        except OSError as exc:
            raise SandboxError(
                "workspace_identity_unverified: "
                f"{self._preflight_identity_diagnostic(repository, image)} "
                f"errno={exc.errno}: {exc}"
            ) from exc
        finally:
            input_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)
        if self._execution_id is not None:
            self._preflight_cache.add(key)

    def _preflight_identity_diagnostic(self, repository: Path, image: str) -> str:
        root_stat = repository.stat()
        host_uid = os.__dict__["getuid"]() if hasattr(os, "getuid") else "n/a"
        host_gid = os.__dict__["getgid"]() if hasattr(os, "getgid") else "n/a"
        image_user = "unknown"
        userns = "unknown"
        try:
            inspected = _run_docker_control(
                ["docker", "image", "inspect", "--format", "{{json .Config.User}}", image],
                timeout_seconds=5,
                docker_host=self.docker_host,
            )
            if inspected.returncode == 0:
                image_user = inspected.stdout.strip()[:256]
        except SandboxCleanupError:
            pass
        try:
            info = _run_docker_control(
                ["docker", "info", "--format", "{{json .SecurityOptions}}"],
                timeout_seconds=5,
                docker_host=self.docker_host,
            )
            if info.returncode == 0:
                userns = info.stdout.strip()[:512]
        except SandboxCleanupError:
            pass
        return (
            f"host_uid={host_uid} host_gid={host_gid} "
            f"mode={oct(root_stat.st_mode & 0o777)} configured_user={self._effective_user()} "
            f"image_id={image} image_user={image_user} userns={userns}"
        )

    @staticmethod
    def _last_json_object(output: str) -> dict[str, Any]:
        for line in reversed(output.splitlines()):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        raise SandboxError("resource_limits_unverified: preflight emitted no JSON evidence")

    def _validate_preflight_evidence(
        self,
        evidence: dict[str, Any],
        token: str,
        output_path: Path,
        user: str | None,
    ) -> None:
        if evidence.get("token") != token or not output_path.is_file():
            raise SandboxError("workspace_identity_unverified: read/write round trip failed")
        if output_path.read_text(encoding="utf-8") != token:
            raise SandboxError("workspace_identity_unverified: output content mismatch")
        if user is not None:
            expected_uid, expected_gid = (int(item) for item in user.split(":"))
            if evidence.get("uid") != expected_uid or evidence.get("gid") != expected_gid:
                raise SandboxError("workspace_identity_unverified: container UID/GID mismatch")
            if os.name != "nt":
                output_stat = output_path.stat()
                if output_stat.st_uid != expected_uid or output_stat.st_gid != expected_gid:
                    raise SandboxError(
                        "workspace_identity_unverified: host file ownership mismatch"
                    )
        expected_memory = str(self.config.memory_mb * 1024 * 1024)
        if evidence.get("memory_max") != expected_memory:
            raise SandboxError("resource_limits_unverified: memory.max mismatch")
        if evidence.get("memory_swap_max") != "0":
            raise SandboxError("resource_limits_unverified: memory.swap.max mismatch")
        if evidence.get("pids_max") != str(self.config.pids_limit):
            raise SandboxError("resource_limits_unverified: pids.max mismatch")
        cpu_max = evidence.get("cpu_max")
        try:
            quota_text, period_text = str(cpu_max).split()
            quota = int(quota_text)
            period = int(period_text)
        except (TypeError, ValueError) as exc:
            raise SandboxError("resource_limits_unverified: cpu.max malformed") from exc
        if period <= 0 or abs((quota / period) - self.config.cpus) > 0.001:
            raise SandboxError("resource_limits_unverified: cpu.max mismatch")

    def _execute_managed(
        self,
        command: list[str],
        repository: Path,
        *,
        timeout_seconds: float,
        max_output_chars: int,
        purpose: Literal["workload", "preflight"],
        image: str | None = None,
    ) -> SandboxResult:
        command_id = str(uuid4())
        execution_id = self._execution_id or f"standalone-{uuid4()}"
        create_command, container_name = self.build_create_command(
            command,
            repository,
            command_id=command_id,
            execution_id=execution_id,
            image=image,
            purpose=purpose,
        )
        control_dir = Path(tempfile.mkdtemp(prefix="patchloop-sandbox-control-"))
        if os.name != "nt":
            os.chmod(control_dir, 0o700)
        try:
            supervisor_environment = _docker_environment(self.docker_host)
            supervisor_environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
            process = subprocess.Popen(
                self._supervisor_command,
                env=supervisor_environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            control_dir.rmdir()
            raise SandboxError("Python supervisor could not be started") from exc
        handle = _SupervisorHandle(process, control_dir)
        self._supervisors[command_id] = handle
        try:
            self._send_supervisor_initialization(
                handle,
                command_id=command_id,
                execution_id=execution_id,
                container_name=container_name,
                create_command=create_command,
                purpose=purpose,
            )
            ready = self._wait_for_ready(handle, command_id, execution_id, timeout=15)
            identity = self._register(
                process,
                backend=self.name,
                container_name=container_name,
                container_id=ready.container_id,
                docker_host=self.docker_host,
                purpose=purpose,
                identity_id=command_id,
                execution_id=execution_id,
            )
            readers_started = threading.Event()
            workload_started = threading.Event()
            captured_output: list[CapturedOutput] = []
            collection_errors: list[BaseException] = []

            def collect() -> None:
                try:
                    captured_output.append(
                        self._communicate(
                            process,
                            identity,
                            timeout_seconds,
                            max_output_chars,
                            finish_on_exit=False,
                            interruption_probe=lambda: (
                                None
                                if not workload_started.is_set()
                                else (
                                    None
                                    if self._interruption_probe is None
                                    else self._interruption_probe()
                                )
                            ),
                            readers_started=readers_started,
                        )
                    )
                except BaseException as exc:
                    collection_errors.append(exc)

            collector = threading.Thread(
                target=collect,
                name=f"sandbox-supervisor-output-{command_id}",
            )
            collector.start()
            if not readers_started.wait(2):
                self._terminate_and_finish(process, identity, "reader_start_failed")
                raise SandboxCleanupError("supervisor output readers did not start")
            if process.stdin is None:
                raise SandboxCleanupError("supervisor control pipe is unavailable")
            try:
                process.stdin.write(b"START\n")
                process.stdin.flush()
            except OSError as exc:
                self._terminate_and_finish(process, identity, "start_signal_failed")
                collector.join(2)
                raise SandboxError("could not start the managed Docker workload") from exc
            workload_started.set()
            collector.join(timeout_seconds + 25)
            if collector.is_alive():
                self._terminate_and_finish(process, identity, "collector_stalled")
                collector.join(2)
                raise SandboxCleanupError("supervisor output collector did not exit")
            if collection_errors:
                raise collection_errors[0]
            if len(captured_output) != 1:
                raise SandboxCleanupError("supervisor output was not captured")
            captured = captured_output[0]
            outcome = self._read_outcome(handle, identity)
            if not outcome.cleanup_confirmed:
                self._finish(
                    identity,
                    ManagedCommandStatus.CLEANUP_FAILED,
                    outcome.reason or outcome.diagnostic,
                )
                raise SandboxCleanupError(
                    outcome.diagnostic or "container cleanup was not confirmed"
                )
            if outcome.status == "cleanup_failed":
                self._finish(
                    identity,
                    ManagedCommandStatus.TERMINATED,
                    outcome.reason or "startup_failed",
                )
                raise SandboxError(outcome.diagnostic or outcome.reason or "Docker startup failed")
            self._finish(identity, ManagedCommandStatus.EXITED)
            output = (captured.stdout_tail + captured.stderr_tail)[-max_output_chars:]
            return SandboxResult(
                exit_code=outcome.exit_code if outcome.exit_code is not None else 1,
                output=output,
                backend=self.name,
                output_truncated=captured.truncated,
                stdout_bytes=captured.stdout_bytes,
                stderr_bytes=captured.stderr_bytes,
                oom_killed=outcome.oom_killed,
            )
        finally:
            self._supervisors.pop(command_id, None)
            if process.stdin is not None:
                with suppress(OSError):
                    process.stdin.close()
            if process.poll() is not None:
                self._remove_control_dir(control_dir)

    def _send_supervisor_initialization(
        self,
        handle: _SupervisorHandle,
        *,
        command_id: str,
        execution_id: str,
        container_name: str,
        create_command: list[str],
        purpose: Literal["workload", "preflight"],
    ) -> None:
        process = handle.process
        if process.stdin is None:
            raise SandboxCleanupError("supervisor control pipe is unavailable")
        payload = {
            "protocol_version": 1,
            "command_id": command_id,
            "execution_id": execution_id,
            "container_name": container_name,
            "docker_host": self.docker_host,
            "purpose": purpose,
            "create_command": create_command,
            "control_dir": str(handle.control_dir),
        }
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        if len(encoded) > 64 * 1024:
            raise SandboxError("supervisor initialization exceeds size limit")
        process.stdin.write(encoded)
        process.stdin.flush()

    def _wait_for_ready(
        self,
        handle: _SupervisorHandle,
        command_id: str,
        execution_id: str,
        *,
        timeout: float,
    ) -> _SupervisorReady:
        deadline = monotonic() + timeout
        path = handle.control_dir / "ready.json"
        while monotonic() < deadline:
            if path.exists():
                ready = _SupervisorReady.model_validate_json(self._read_control_file(path))
                if (
                    ready.protocol_version != 1
                    or ready.command_id != command_id
                    or ready.execution_id != execution_id
                    or ready.container_name != f"patchloop-{command_id}"
                    or ready.docker_host != self.docker_host
                ):
                    raise SandboxCleanupError("supervisor READY identity mismatch")
                return ready
            if handle.process.poll() is not None:
                outcome_path = handle.control_dir / "outcome.json"
                detail = "supervisor exited before READY"
                if outcome_path.exists():
                    raw_outcome = self._read_control_file(outcome_path)
                    detail = raw_outcome[-8192:]
                    try:
                        outcome = _SupervisorOutcome.model_validate_json(raw_outcome)
                    except ValueError as exc:
                        raise SandboxCleanupError(
                            "supervisor emitted an invalid startup outcome"
                        ) from exc
                    if not outcome.cleanup_confirmed:
                        raise SandboxCleanupError(
                            outcome.diagnostic or "startup cleanup was not confirmed"
                        )
                raise SandboxError(detail)
            sleep(0.05)
        if handle.process.stdin is not None:
            handle.process.stdin.close()
        try:
            handle.process.wait(timeout=20)
        except subprocess.TimeoutExpired as exc:
            raise SandboxCleanupError("supervisor startup cleanup timed out") from exc
        raise SandboxError("Docker supervisor did not become ready within 15 seconds")

    @staticmethod
    def _read_control_file(path: Path) -> str:
        if path.stat().st_size > 16 * 1024:
            raise SandboxCleanupError("supervisor control file exceeds size limit")
        return path.read_text(encoding="utf-8")

    def _read_outcome(
        self, handle: _SupervisorHandle, identity: ManagedCommandIdentity
    ) -> _SupervisorOutcome:
        path = handle.control_dir / "outcome.json"
        if not path.exists():
            raise SandboxCleanupError("supervisor exited without a valid outcome")
        outcome = _SupervisorOutcome.model_validate_json(self._read_control_file(path))
        if (
            outcome.protocol_version != 1
            or outcome.command_id != identity.id
            or outcome.execution_id != identity.execution_id
            or outcome.container_id != identity.container_id
        ):
            raise SandboxCleanupError("supervisor outcome identity mismatch")
        return outcome

    @staticmethod
    def _remove_control_dir(control_dir: Path) -> None:
        for name in ("ready.json", "outcome.json"):
            (control_dir / name).unlink(missing_ok=True)
        with suppress(OSError):
            control_dir.rmdir()

    def _terminate_process(
        self, process: subprocess.Popen[bytes], identity: ManagedCommandIdentity
    ) -> None:
        handle = self._supervisors.get(identity.id)
        direct_cleanup = False
        if handle is not None and process.poll() is None:
            with handle.lock:
                if not handle.stop_sent and process.stdin is not None:
                    try:
                        process.stdin.write(b"STOP\n")
                        process.stdin.flush()
                        handle.stop_sent = True
                    except OSError:
                        pass
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            _remove_verified_container(identity)
            direct_cleanup = True
            process.kill()
            process.wait(timeout=5)
        if handle is not None and not direct_cleanup:
            outcome = self._read_outcome(handle, identity)
            if not outcome.cleanup_confirmed:
                raise SandboxCleanupError(outcome.diagnostic or "supervisor cleanup failed")
        elif (
            _inspect_docker_container(
                identity.container_id or identity.container_name or "",
                docker_host=identity.docker_host,
            )
            is not None
        ):
            _remove_verified_container(identity)


def create_command_sandbox(
    execution: TaskExecutionConfig,
) -> DockerSandbox | LocalProcessSandbox:
    if execution.sandbox_backend == "local":
        if execution.sandbox_workspace_limit_mb is not None:
            raise ValueError("workspace limits require the Docker sandbox")
        return LocalProcessSandbox()
    if execution.sandbox_backend == "docker":
        return DockerSandbox(
            DockerSandboxConfig(
                image=execution.sandbox_image,
                workspace_limit_mb=execution.sandbox_workspace_limit_mb,
                workspace_inode_limit=execution.sandbox_workspace_inode_limit,
            )
        )
    raise ValueError(f"unsupported sandbox backend: {execution.sandbox_backend}")
