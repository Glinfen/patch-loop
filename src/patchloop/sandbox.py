"""Command execution backends with a fail-closed Docker sandbox."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


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


class ManagedCommandIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    execution_id: str = Field(min_length=1, max_length=128)
    backend: str = Field(min_length=1, max_length=32)
    process_id: int = Field(gt=0)
    process_start_marker: str = Field(min_length=1, max_length=256)
    container_name: str | None = Field(default=None, min_length=1, max_length=128)
    status: ManagedCommandStatus = ManagedCommandStatus.RUNNING
    cleanup_reason: str | None = Field(default=None, max_length=256)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


CommandStarted = Callable[[ManagedCommandIdentity], None]
CommandFinished = Callable[[ManagedCommandIdentity], None]
InterruptionProbe = Callable[[], str | None]


class SandboxResult(BaseModel):
    exit_code: int
    output: str
    backend: str


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


def _process_start_marker(process: subprocess.Popen[str]) -> str:
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


def _docker_command_id(container_name: str) -> str | None:
    inspected = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            '{{ index .Config.Labels "patchloop.command_id" }}',
            container_name,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
        check=False,
        shell=False,
    )
    if inspected.returncode == 0:
        return inspected.stdout.strip()
    error = inspected.stderr.strip()
    if "No such object" in error or "No such container" in error:
        return None
    raise SandboxCleanupError(error or "docker inspect failed")


def _create_windows_kill_job(process: subprocess.Popen[str]) -> int:
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
        if identity.container_name is None:
            raise SandboxCleanupError("managed Docker command has no container identity")
        command_id = _docker_command_id(identity.container_name)
        if command_id is not None:
            if command_id != identity.id:
                raise SandboxCleanupError("container identity changed before recovery cleanup")
            removed = subprocess.run(
                ["docker", "rm", "--force", identity.container_name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
                shell=False,
            )
            if removed.returncode != 0:
                raise SandboxCleanupError(removed.stderr.strip() or "docker rm failed")
            if _docker_command_id(identity.container_name) is not None:
                raise SandboxCleanupError("managed container remained after recovery cleanup")
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
        self._active: dict[str, tuple[subprocess.Popen[str], ManagedCommandIdentity]] = {}
        self._active_lock = threading.Lock()

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
        process: subprocess.Popen[str],
        *,
        backend: str,
        container_name: str | None = None,
        identity_id: str | None = None,
    ) -> ManagedCommandIdentity:
        identity = ManagedCommandIdentity(
            id=identity_id or str(uuid4()),
            execution_id=self._execution_id or f"standalone-{uuid4()}",
            backend=backend,
            process_id=process.pid,
            process_start_marker=_process_start_marker(process),
            container_name=container_name,
        )
        with self._active_lock:
            self._active[identity.id] = (process, identity)
        try:
            if self._command_started is not None:
                self._command_started(identity)
        except BaseException:
            try:
                self._terminate_process(process, identity)
            finally:
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
        finished = identity.model_copy(
            update={
                "status": status,
                "cleanup_reason": reason,
                "updated_at": datetime.now(UTC),
            }
        )
        with self._active_lock:
            self._active.pop(identity.id, None)
        if self._command_finished is not None:
            try:
                self._command_finished(finished)
            except Exception as exc:
                raise SandboxCleanupError(
                    f"could not persist managed command outcome: {identity.id}"
                ) from exc

    def _terminate_and_finish(
        self,
        process: subprocess.Popen[str],
        identity: ManagedCommandIdentity,
        reason: str,
    ) -> None:
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
        process: subprocess.Popen[str],
        identity: ManagedCommandIdentity,
        timeout_seconds: float,
    ) -> tuple[str, str]:
        deadline = monotonic() + timeout_seconds
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                self._terminate_and_finish(process, identity, "timeout")
                raise SandboxTimeoutError(
                    f"{identity.backend} command timed out after {timeout_seconds:g} seconds"
                )
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                reason = None if self._interruption_probe is None else self._interruption_probe()
                if reason is None:
                    continue
                self._terminate_and_finish(process, identity, reason)
                raise SandboxInterruptedError(reason) from None
            self._complete_process_scope(process, identity)
            self._finish(identity, ManagedCommandStatus.EXITED)
            return stdout, stderr

    def _terminate_process(
        self, process: subprocess.Popen[str], identity: ManagedCommandIdentity
    ) -> None:
        raise NotImplementedError

    def _complete_process_scope(
        self, process: subprocess.Popen[str], identity: ManagedCommandIdentity
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
            text=True,
            encoding="utf-8",
            errors="replace",
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
        stdout, stderr = self._communicate(process, identity, timeout_seconds)
        return SandboxResult(
            exit_code=process.returncode,
            output=(stdout + stderr)[-max_output_chars:],
            backend=self.name,
        )

    def _terminate_process(
        self, process: subprocess.Popen[str], identity: ManagedCommandIdentity
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
        self, process: subprocess.Popen[str], identity: ManagedCommandIdentity
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


class DockerSandbox(_ManagedSandboxBase):
    name = "docker"

    def __init__(self, config: DockerSandboxConfig | None = None) -> None:
        super().__init__()
        self.config = config or DockerSandboxConfig()

    def build_command(self, command: list[str], repository: Path) -> list[str]:
        repository = repository.resolve(strict=True)
        network = "bridge" if self.config.network_enabled else "none"
        container_command = list(command)
        executable = Path(container_command[0]).name.casefold()
        if executable in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}:
            container_command[0] = "python"
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "--cpus",
            f"{self.config.cpus:g}",
            "--memory",
            f"{self.config.memory_mb}m",
            "--pids-limit",
            str(self.config.pids_limit),
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--mount",
            f"type=bind,source={repository},target=/workspace",
            "--workdir",
            "/workspace",
            self.config.image,
            *container_command,
        ]

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
        command_id = str(uuid4())
        docker_command, container_name = self.build_managed_command(
            command,
            repository,
            command_id=command_id,
            execution_id=self._execution_id or "standalone",
        )
        try:
            process = subprocess.Popen(
                docker_command,
                env=_minimal_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
        except FileNotFoundError as exc:
            raise SandboxError("Docker is required but was not found; execution denied") from exc
        identity = self._register(
            process,
            backend=self.name,
            container_name=container_name,
            identity_id=command_id,
        )
        stdout, stderr = self._communicate(process, identity, timeout_seconds)
        output = (stdout + stderr)[-max_output_chars:]
        if process.returncode in {125, 126, 127}:
            raise SandboxError(f"Docker sandbox failed to start: {output.strip()}")
        return SandboxResult(
            exit_code=process.returncode,
            output=output,
            backend=self.name,
        )

    def _terminate_process(
        self, process: subprocess.Popen[str], identity: ManagedCommandIdentity
    ) -> None:
        container_name = identity.container_name
        if container_name is None:
            raise SandboxCleanupError("managed Docker command has no container identity")
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                '{{ index .Config.Labels "patchloop.command_id" }}',
                container_name,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            shell=False,
        )
        if inspected.returncode == 0:
            if inspected.stdout.strip() != identity.id:
                raise SandboxCleanupError("container identity changed before cleanup")
            removed = subprocess.run(
                ["docker", "rm", "--force", container_name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                shell=False,
            )
            if removed.returncode != 0:
                raise SandboxCleanupError(removed.stderr.strip() or "docker rm failed")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait(timeout=5)
            raise SandboxCleanupError("Docker client did not exit after container cleanup") from exc
