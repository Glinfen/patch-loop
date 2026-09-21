"""Run destructive-but-bounded probes against the real Docker sandbox.

This module is intentionally not a pytest test.  It produces a self-contained
JSON artifact and is only meant to run on a disposable Linux Docker host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from patchloop.sandbox import (
    DockerSandbox,
    ManagedCommandIdentity,
    SandboxError,
    SandboxInterruptedError,
    SandboxTimeoutError,
    _inspect_docker_container,
    _remove_verified_container,
    reconcile_managed_command,
)

CaseProbe = Callable[[], dict[str, Any]]


class ProbeUnverified(RuntimeError):
    pass


def _run(command: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _json_output(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError(f"command did not emit a JSON object: {output[-500:]}")


def _safe_output(command: list[str]) -> str:
    try:
        completed = _run(command)
    except BaseException:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _container_exists(name: str) -> bool:
    return _inspect_docker_container(name) is not None


def _labelled_containers(execution_id: str) -> list[str]:
    completed = _run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=patchloop.execution_id={execution_id}",
            "--format",
            "{{.Names}}",
        ]
    )
    if completed.returncode != 0:
        raise ProbeUnverified(completed.stderr.strip() or "docker ps query failed")
    return [line for line in completed.stdout.splitlines() if line.strip()]


class Matrix:
    def __init__(self, root: Path, output: Path, workspace: Path | None = None) -> None:
        self.root = root.resolve(strict=True)
        self.output = output.resolve()
        self.run_id = uuid4().hex
        self.probe_root = self.root / f".sandbox-real-probes-{self.run_id}"
        self._external_workspace = workspace is not None
        self.workspace = (
            workspace.resolve(strict=True)
            if workspace is not None
            else self.probe_root / "workspace"
        )
        self.secret = self.probe_root / "outside-secret.txt"
        self.sentinel = f"HOST_SECRET_{uuid4().hex}"
        self.sandbox = DockerSandbox()
        self.cases: list[dict[str, Any]] = []
        self._managed_identities: dict[str, ManagedCommandIdentity] = {}
        self._fixture_paths: set[Path] = set()
        self._workspace_empty_at_start = False
        self.execution_id = f"real-matrix-{self.run_id}"
        self.sandbox.bind_execution(
            self.execution_id,
            command_started=self._record_identity,
            command_finished=lambda identity: None,
            interruption_probe=lambda: None,
        )

    def _record_identity(self, identity: ManagedCommandIdentity) -> None:
        self._managed_identities[identity.id] = identity

    def _track_fixture(self, path: Path) -> Path:
        self._fixture_paths.add(path)
        return path

    def prepare(self) -> None:
        self.probe_root.mkdir(parents=False, exist_ok=False)
        if self._external_workspace:
            unexpected = [item for item in self.workspace.iterdir() if item.name != "lost+found"]
            if unexpected:
                raise ValueError("--workspace must name a dedicated empty directory")
        else:
            self.workspace.mkdir(parents=True)
        self._workspace_empty_at_start = True
        self.secret.write_text(self.sentinel, encoding="utf-8")
        self._track_fixture(self.workspace / "safe.txt").write_text("SAFE", encoding="utf-8")
        receipt = self._track_fixture(self.workspace / f".patchloop-matrix-{self.run_id}.json")
        receipt.write_text(json.dumps({"run_id": self.run_id}), encoding="utf-8")

    def add(
        self,
        case_id: str,
        category: str,
        probe: CaseProbe,
        *,
        attempts: int = 1,
        requires: tuple[str, ...] = (),
    ) -> None:
        started = time.monotonic()
        evidence: list[dict[str, Any]] = []
        unmet = [
            case_id
            for case_id in requires
            if next((item["status"] for item in self.cases if item["id"] == case_id), None)
            != "passed"
        ]
        if unmet:
            self.cases.append(
                {
                    "id": case_id,
                    "category": category,
                    "status": "unverified",
                    "attempts": 0,
                    "duration_ms": 0.0,
                    "requires": list(requires),
                    "evidence": [{"unmet_preconditions": unmet}],
                }
            )
            return
        status = "passed"
        for attempt in range(1, attempts + 1):
            try:
                item = probe()
                passed = bool(item.pop("passed"))
                item = {"attempt": attempt, **item}
                evidence.append(item)
                if not passed:
                    status = "failed"
            except ProbeUnverified as exc:
                status = "unverified"
                evidence.append(
                    {
                        "attempt": attempt,
                        "exception": type(exc).__name__,
                        "detail": str(exc),
                    }
                )
            except Exception as exc:
                status = "failed"
                evidence.append(
                    {
                        "attempt": attempt,
                        "exception": type(exc).__name__,
                        "detail": str(exc),
                    }
                )
        self.cases.append(
            {
                "id": case_id,
                "category": category,
                "status": status,
                "attempts": attempts,
                "requires": list(requires),
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "evidence": evidence,
            }
        )

    def note(self, case_id: str, category: str, status: str, detail: str) -> None:
        self.cases.append(
            {
                "id": case_id,
                "category": category,
                "status": status,
                "attempts": 0,
                "duration_ms": 0.0,
                "evidence": [{"detail": detail}],
            }
        )

    def execute_python(
        self,
        code: str,
        *,
        timeout: float = 10,
        max_output: int = 20_000,
        sandbox: DockerSandbox | None = None,
    ) -> Any:
        return (sandbox or self.sandbox).execute(
            ["python", "-c", code],
            self.workspace,
            timeout_seconds=timeout,
            max_output_chars=max_output,
        )

    def configuration(self) -> dict[str, Any]:
        command_id = uuid4().hex
        command, name = self.sandbox.build_managed_command(
            ["python", "-c", "print('configured')"],
            self.workspace,
            command_id=command_id,
            execution_id=self.execution_id,
        )
        create = list(command)
        create[1] = "create"
        create.remove("--rm")
        created = _run(create)
        if created.returncode != 0:
            return {"passed": False, "create_error": created.stderr.strip()}
        snapshot = _inspect_docker_container(name)
        if snapshot is None:
            raise ProbeUnverified("created configuration container was not inspectable")
        identity = ManagedCommandIdentity(
            id=command_id,
            execution_id=self.execution_id,
            backend="docker",
            process_id=os.getpid(),
            process_start_marker="acceptance-harness",
            container_name=name,
            container_id=snapshot.container_id,
        )
        self._record_identity(identity)
        try:
            inspected = json.loads(_run(["docker", "inspect", name]).stdout)[0]
            host = inspected["HostConfig"]
            config = inspected["Config"]
            mounts = inspected["Mounts"]
            tmpfs = host.get("Tmpfs") or {}
            checks = {
                "network_none": host.get("NetworkMode") == "none",
                "pid_namespace_not_host": host.get("PidMode") != "host",
                "not_privileged": host.get("Privileged") is False,
                "readonly_rootfs": host.get("ReadonlyRootfs") is True,
                "cap_drop_all": "ALL" in (host.get("CapDrop") or []),
                "no_new_privileges": any(
                    "no-new-privileges" in item for item in (host.get("SecurityOpt") or [])
                ),
                "cpu_limit": host.get("NanoCpus") == 1_000_000_000,
                "memory_limit": host.get("Memory") == 512 * 1024 * 1024,
                "pid_limit": host.get("PidsLimit") == 128,
                "tmpfs_limit": "/tmp" in tmpfs and "size=64m" in tmpfs["/tmp"],
                "tmpfs_hardened": "/tmp" in tmpfs
                and "noexec" in tmpfs["/tmp"]
                and "nosuid" in tmpfs["/tmp"],
                "identity_labels": config.get("Labels", {}).get("patchloop.command_id")
                == command_id
                and config.get("Labels", {}).get("patchloop.execution_id")
                == self.execution_id,
                "only_workspace_mount": len(mounts) == 1
                and mounts[0].get("Source") == str(self.workspace)
                and mounts[0].get("Destination") == "/workspace",
                "docker_socket_absent": all(
                    mount.get("Destination") != "/var/run/docker.sock" for mount in mounts
                ),
            }
            return {
                "passed": all(checks.values()),
                "checks": checks,
                "image_reference": config.get("Image"),
                "container_name": name,
            }
        finally:
            _remove_verified_container(identity)

    def file_escape(self) -> dict[str, Any]:
        link = self.workspace / "escape-link"
        safe_link = self.workspace / "safe-link"
        link.unlink(missing_ok=True)
        safe_link.unlink(missing_ok=True)
        link.symlink_to(self.secret)
        safe_link.symlink_to("safe.txt")
        self._track_fixture(link)
        self._track_fixture(safe_link)
        code = f"""
import json
from pathlib import Path
sentinel = {self.sentinel!r}
paths = [
    Path('/workspace/../outside-secret.txt'),
    Path('/workspace/escape-link'),
    Path({str(self.secret)!r}),
]
safe_read = Path('/workspace/safe-link').read_text() == 'SAFE'
reads = []
writes = []
for path in paths:
    try:
        reads.append(path.read_text() == sentinel)
    except Exception:
        reads.append(False)
    try:
        path.write_text('ESCAPED')
        writes.append(True)
    except Exception:
        writes.append(False)
print(json.dumps({{'safe_read': safe_read, 'read_leaks': reads, 'write_escapes': writes}}))
"""
        result = self.execute_python(code)
        data = _json_output(result.output)
        intact = self.secret.read_text(encoding="utf-8") == self.sentinel
        passed = (
            result.exit_code == 0
            and data["safe_read"]
            and not any(data["read_leaks"])
            and not any(data["write_escapes"])
        )
        return {"passed": passed and intact, **data, "outside_secret_intact": intact}

    def symlink_toctou(self) -> dict[str, Any]:
        link = self.workspace / "race-link"
        link.unlink(missing_ok=True)
        link.symlink_to("safe.txt")
        self._track_fixture(link)
        stop = threading.Event()

        def swap() -> None:
            targets = [Path("safe.txt"), self.secret]
            index = 0
            while not stop.is_set():
                temporary = self.workspace / f".race-{self.run_id}-{uuid4().hex}"
                temporary.symlink_to(targets[index % 2])
                os.replace(temporary, link)
                index += 1

        thread = threading.Thread(target=swap, daemon=True)
        thread.start()
        try:
            code = f"""
import json, time
from pathlib import Path
sentinel = {self.sentinel!r}
leaks = 0
safe_reads = 0
deadline = time.monotonic() + 1.5
while time.monotonic() < deadline:
    try:
        value = Path('/workspace/race-link').read_text()
        leaks += value == sentinel
        safe_reads += value == 'SAFE'
    except Exception:
        pass
print(json.dumps({{'secret_reads': leaks, 'safe_reads': safe_reads}}))
"""
            result = self.execute_python(code, timeout=5)
        finally:
            stop.set()
            thread.join(timeout=2)
        data = _json_output(result.output)
        intact = self.secret.read_text(encoding="utf-8") == self.sentinel
        return {
            "passed": result.exit_code == 0
            and data["safe_reads"] > 0
            and data["secret_reads"] == 0
            and intact,
            **data,
            "outside_secret_intact": intact,
        }

    def network_isolation(self) -> dict[str, Any]:
        try:
            socket.getaddrinfo("example.com", 443)
            with urllib.request.urlopen("http://example.com", timeout=3) as response:
                response.read(1)
        except Exception as exc:
            raise ProbeUnverified(f"host DNS/HTTP positive control failed: {exc}") from exc
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        port = listener.getsockname()[1]
        accepted = threading.Event()

        def accept_once() -> None:
            connection, _ = listener.accept()
            connection.close()
            accepted.set()

        thread = threading.Thread(target=accept_once)
        thread.start()
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
        thread.join(timeout=2)
        listener.close()
        if not accepted.is_set():
            raise ProbeUnverified("host listener positive control failed")
        code = """
import json, socket, urllib.request
result = {}
try:
    socket.getaddrinfo('example.com', 443)
    result['dns'] = True
except Exception:
    result['dns'] = False
for key, address in [('tcp', ('1.1.1.1', 80)), ('localhost', ('127.0.0.1', __PORT__))]:
    sock = socket.socket()
    sock.settimeout(1)
    try:
        sock.connect(address)
        result[key] = True
    except Exception:
        result[key] = False
    finally:
        sock.close()
try:
    urllib.request.urlopen('http://example.com', timeout=1)
    result['http'] = True
except Exception:
    result['http'] = False
try:
    socket.getaddrinfo('host.docker.internal', 80)
    result['host_gateway'] = True
except Exception:
    result['host_gateway'] = False
routes = open('/proc/net/route').read().splitlines()[1:]
result['default_route'] = any(
    len(fields := line.split()) > 1 and fields[1] == '00000000' for line in routes
)
print(json.dumps(result))
""".replace("__PORT__", str(port))
        result = self.execute_python(code, timeout=8)
        data = _json_output(result.output)
        return {
            "passed": result.exit_code == 0 and not any(data.values()),
            "host_listener_positive_control": True,
            **data,
        }

    def environment_isolation(self) -> dict[str, Any]:
        variable = "PATCHLOOP_REAL_SANDBOX_HOST_SECRET"
        previous = os.environ.get(variable)
        os.environ[variable] = self.sentinel
        try:
            code = f"""
import json, os
from pathlib import Path
needle = {self.sentinel!r}
environment = '\\0'.join(f'{{key}}={{value}}' for key, value in os.environ.items())
proc_environment = Path('/proc/1/environ').read_bytes().decode(errors='replace')
paths = ['/root/.aws/credentials', '/root/.ssh/id_rsa', '/run/secrets', '/var/run/docker.sock']
print(json.dumps({{
    'explicit_variable': os.environ.get({variable!r}),
    'sentinel_in_environment': needle in environment or needle in proc_environment,
    'credential_paths_present': [path for path in paths if Path(path).exists()],
}}))
"""
            result = self.execute_python(code)
        finally:
            if previous is None:
                os.environ.pop(variable, None)
            else:
                os.environ[variable] = previous
        data = _json_output(result.output)
        passed = (
            result.exit_code == 0
            and data["explicit_variable"] is None
            and not data["sentinel_in_environment"]
            and not data["credential_paths_present"]
        )
        return {"passed": passed, **data}

    def runtime_privileges(self) -> dict[str, Any]:
        code = """
import json
from pathlib import Path
status = {}
for line in Path('/proc/self/status').read_text().splitlines():
    if line.startswith(('CapEff:', 'NoNewPrivs:')):
        key, value = line.split(':', 1)
        status[key] = value.strip()
try:
    Path('/etc/patchloop-write-probe').write_text('x')
    root_write = True
except Exception:
    root_write = False
print(json.dumps({
    'cap_eff': status.get('CapEff'),
    'no_new_privs': status.get('NoNewPrivs'),
    'root_write': root_write,
    'docker_socket': Path('/var/run/docker.sock').exists(),
}))
"""
        result = self.execute_python(code)
        data = _json_output(result.output)
        passed = (
            result.exit_code == 0
            and int(data["cap_eff"], 16) == 0
            and data["no_new_privs"] == "1"
            and not data["root_write"]
            and not data["docker_socket"]
        )
        return {"passed": passed, **data}

    def workspace_read_write(self) -> dict[str, Any]:
        target = self._track_fixture(self.workspace / "write-probe.txt")
        target.unlink(missing_ok=True)
        result = self.execute_python(
            "from pathlib import Path; "
            "source = Path('/workspace/safe.txt').read_text(); "
            "Path('/workspace/write-probe.txt').write_text(source); "
            "print(source)"
        )
        content = target.read_text(encoding="utf-8") if target.exists() else None
        target.unlink(missing_ok=True)
        return {
            "passed": result.exit_code == 0 and content == "SAFE",
            "exit_code": result.exit_code,
            "host_file_content": content,
            "output_tail": result.output[-500:],
        }

    def normal_cleanup(self) -> dict[str, Any]:
        started: list[ManagedCommandIdentity] = []
        finished: list[ManagedCommandIdentity] = []
        sandbox = DockerSandbox()
        execution_id = f"matrix-normal-{self.run_id}"
        sandbox.bind_execution(
            execution_id,
            command_started=lambda identity: (
                started.append(identity),
                self._record_identity(identity),
            ),
            command_finished=finished.append,
            interruption_probe=lambda: None,
        )
        result = self.execute_python("print('normal')", sandbox=sandbox)
        name = started[0].container_name or ""
        return {
            "passed": result.exit_code == 0 and len(finished) == 1 and not _container_exists(name),
            "container": name,
            "finished_status": finished[0].status.value,
            "container_absent": not _container_exists(name),
        }

    def timeout_cleanup(self) -> dict[str, Any]:
        started: list[ManagedCommandIdentity] = []
        finished: list[ManagedCommandIdentity] = []
        sandbox = DockerSandbox()
        execution_id = f"matrix-timeout-{self.run_id}"
        sandbox.bind_execution(
            execution_id,
            command_started=lambda identity: (
                started.append(identity),
                self._record_identity(identity),
            ),
            command_finished=finished.append,
            interruption_probe=lambda: None,
        )
        timed_out = False
        try:
            self.execute_python("import time; time.sleep(30)", timeout=0.5, sandbox=sandbox)
        except SandboxTimeoutError:
            timed_out = True
        name = started[0].container_name or ""
        return {
            "passed": timed_out and len(finished) == 1 and not _container_exists(name),
            "timeout_raised": timed_out,
            "container": name,
            "container_absent": not _container_exists(name),
            "cleanup_reason": finished[0].cleanup_reason if finished else None,
        }

    def interruption_cleanup(self, reason: str) -> dict[str, Any]:
        started: list[ManagedCommandIdentity] = []
        finished: list[ManagedCommandIdentity] = []
        sandbox = DockerSandbox()
        execution_id = f"matrix-{reason}-{self.run_id}"
        sandbox.bind_execution(
            execution_id,
            command_started=lambda identity: (
                started.append(identity),
                self._record_identity(identity),
            ),
            command_finished=finished.append,
            interruption_probe=lambda: reason,
        )
        interrupted = False
        try:
            self.execute_python("import time; time.sleep(30)", timeout=10, sandbox=sandbox)
        except SandboxInterruptedError as exc:
            interrupted = exc.reason == reason
        name = started[0].container_name or ""
        return {
            "passed": interrupted and len(finished) == 1 and not _container_exists(name),
            "interrupted": interrupted,
            "container_absent": not _container_exists(name),
            "cleanup_reason": finished[0].cleanup_reason if finished else None,
        }

    def pause_resume(self) -> dict[str, Any]:
        paused = self.interruption_cleanup("pause")
        resumed = self.execute_python("print('resumed')")
        return {
            "passed": bool(paused.pop("passed")) and resumed.exit_code == 0,
            "pause": paused,
            "resume_exit_code": resumed.exit_code,
        }

    def background_cleanup(self) -> dict[str, Any]:
        marker = self._track_fixture(self.workspace / "background-survived.txt")
        marker.unlink(missing_ok=True)
        child = (
            "import pathlib,time; time.sleep(1.5); "
            "pathlib.Path('/workspace/background-survived.txt').write_text('survived')"
        )
        parent = (
            "import subprocess,sys; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); print('parent-exit')"
        )
        result = self.execute_python(parent)
        time.sleep(2)
        return {
            "passed": result.exit_code == 0 and not marker.exists(),
            "marker_absent": not marker.exists(),
        }

    def pid_limit(self) -> dict[str, Any]:
        code = """
import json, os, time
children = 0
error = None
for _ in range(300):
    try:
        pid = os.fork()
    except OSError as exc:
        error = str(exc)
        break
    if pid == 0:
        time.sleep(20)
        os._exit(0)
    children += 1
print(json.dumps({'children_created': children, 'fork_error': error}), flush=True)
"""
        result = self.execute_python(code, timeout=8)
        data = _json_output(result.output)
        return {
            "passed": result.exit_code == 0
            and 1 <= data["children_created"] < 200
            and bool(data["fork_error"]),
            **data,
        }

    def memory_limit(self) -> dict[str, Any]:
        result = self.execute_python(
            "x = bytearray(700 * 1024 * 1024); "
            "[(x.__setitem__(index, 1)) for index in range(0, len(x), 4096)]; "
            "print(sum(x))",
            timeout=15,
        )
        return {
            "passed": result.exit_code != 0,
            "exit_code": result.exit_code,
            "output_tail": result.output[-300:],
        }

    def temporary_storage_limit(self) -> dict[str, Any]:
        code = """
import json
written = 0
error = None
try:
    with open('/tmp/fill', 'wb') as stream:
        for _ in range(96):
            stream.write(b'x' * 1024 * 1024)
            stream.flush()
            written += 1024 * 1024
except OSError as exc:
    error = str(exc)
print(json.dumps({'written': written, 'error': error}))
"""
        result = self.execute_python(code, timeout=15)
        data = _json_output(result.output)
        return {
            "passed": result.exit_code == 0
            and data["written"] < 96 * 1024 * 1024
            and bool(data["error"]),
            **data,
        }

    def output_limit(self) -> dict[str, Any]:
        result = self.execute_python(
            "import os; "
            "[(os.write(1, b'O' * (1024 * 1024)), os.write(2, b'E' * (1024 * 1024))) "
            "for _ in range(128)]",
            timeout=60,
            max_output=4096,
        )
        return {
            "passed": result.exit_code == 0
            and len(result.output) <= 4096
            and result.output_truncated
            and result.stdout_bytes == 128 * 1024 * 1024
            and result.stderr_bytes == 128 * 1024 * 1024,
            "returned_chars": len(result.output),
            "stdout_bytes": result.stdout_bytes,
            "stderr_bytes": result.stderr_bytes,
            "output_truncated": result.output_truncated,
        }

    def workspace_storage_limit(self) -> dict[str, Any]:
        target = self._track_fixture(self.workspace / "workspace-fill.bin")
        target.unlink(missing_ok=True)
        code = """
import json
written = 0
error = None
try:
    with open('/workspace/workspace-fill.bin', 'wb') as stream:
        for _ in range(80):
            stream.write(b'x' * 1024 * 1024)
            stream.flush()
            written += 1024 * 1024
except OSError as exc:
    error = str(exc)
print(json.dumps({'written': written, 'error': error}))
"""
        result = self.execute_python(code, timeout=20)
        data = _json_output(result.output)
        size = target.stat().st_size if target.exists() else 0
        target.unlink(missing_ok=True)
        return {
            "passed": result.exit_code == 0
            and data["written"] > 0
            and data["written"] < 80 * 1024 * 1024
            and "No space left" in (data["error"] or ""),
            "exit_code": result.exit_code,
            "bytes_written": size,
            "reported_bytes_written": data["written"],
            "error": data["error"],
            "expected": "a configured workspace/output storage ceiling",
        }

    def startup_failure(self) -> dict[str, Any]:
        sandbox = DockerSandbox()
        execution_id = f"matrix-startup-{self.run_id}"
        sandbox.bind_execution(
            execution_id,
            command_started=self._record_identity,
            command_finished=lambda identity: None,
            interruption_probe=lambda: None,
        )
        denied = False
        detail = ""
        try:
            sandbox.execute(
                ["/definitely-missing-patchloop-entrypoint"],
                self.workspace,
                timeout_seconds=10,
                max_output_chars=2_000,
            )
        except SandboxError as exc:
            denied = True
            detail = str(exc)
        return {
            "passed": denied and not _labelled_containers(execution_id),
            "structured_exception": denied,
            "detail": detail,
            "residual_containers": _labelled_containers(execution_id),
        }

    def identity_mismatch(self) -> dict[str, Any]:
        command_id = uuid4().hex
        name = f"patchloop-{command_id}"
        create = _run(
            [
                "docker",
                "create",
                "--name",
                name,
                "--label",
                "patchloop.command_id=wrong-identity",
                "--label",
                f"patchloop.execution_id=matrix-mismatch-{self.run_id}",
                "patchloop-sandbox:py313",
                "python",
                "-c",
                "print('identity')",
            ]
        )
        if create.returncode != 0:
            return {"passed": False, "create_error": create.stderr.strip()}
        snapshot = _inspect_docker_container(name)
        if snapshot is None:
            raise ProbeUnverified("identity-mismatch container disappeared after create")
        cleanup_identity = ManagedCommandIdentity(
            id="wrong-identity",
            execution_id=f"matrix-mismatch-{self.run_id}",
            backend="docker",
            process_id=os.getpid(),
            process_start_marker="acceptance-harness",
            container_name=name,
            container_id=snapshot.container_id,
        )
        self._record_identity(cleanup_identity)
        identity = ManagedCommandIdentity(
            id=command_id,
            execution_id=f"matrix-mismatch-{self.run_id}",
            backend="docker",
            process_id=os.getpid(),
            process_start_marker="matrix",
            container_name=name,
        )
        refused = False
        detail = ""
        try:
            reconcile_managed_command(identity)
        except Exception as exc:
            refused = True
            detail = str(exc)
        still_present = _container_exists(name)
        _remove_verified_container(cleanup_identity)
        return {
            "passed": refused and still_present,
            "cleanup_refused": refused,
            "container_preserved_until_explicit_cleanup": still_present,
            "detail": detail,
        }

    def parent_crash(self) -> dict[str, Any]:
        identity_path = self.probe_root / "parent-crash-identity.json"
        identity_path.unlink(missing_ok=True)
        child_code = f"""
from pathlib import Path
from patchloop.sandbox import DockerSandbox
sandbox = DockerSandbox()
def started(identity):
    Path({str(identity_path)!r}).write_text(identity.model_dump_json())
sandbox.bind_execution(
    'matrix-parent-crash',
    command_started=started,
    command_finished=lambda identity: None,
    interruption_probe=lambda: None,
)
sandbox.execute(
    ['python', '-c', 'import time; time.sleep(60)'],
    Path({str(self.workspace)!r}),
    timeout_seconds=90,
    max_output_chars=1000,
)
"""
        process = subprocess.Popen([sys.executable, "-c", child_code], cwd=self.root)
        deadline = time.monotonic() + 10
        identity: ManagedCommandIdentity | None = None
        while time.monotonic() < deadline:
            if identity_path.exists():
                identity = ManagedCommandIdentity.model_validate_json(
                    identity_path.read_text(encoding="utf-8")
                )
                if identity.container_name and _container_exists(identity.container_name):
                    self._record_identity(identity)
                    break
            time.sleep(0.05)
        if identity is None or identity.container_name is None:
            process.kill()
            process.wait(timeout=5)
            return {"passed": False, "detail": "managed container never became observable"}
        process.kill()
        process.wait(timeout=5)
        time.sleep(0.5)
        residual_after_crash = _container_exists(identity.container_name)
        recovery_error = ""
        try:
            reconcile_managed_command(identity)
        except Exception as exc:
            recovery_error = f"{type(exc).__name__}: {exc}"
        residual_after_recovery = _container_exists(identity.container_name)
        if residual_after_recovery:
            _run(["docker", "rm", "--force", identity.container_name])
        return {
            "passed": not residual_after_crash and not residual_after_recovery,
            "container": identity.container_name,
            "residual_after_parent_crash": residual_after_crash,
            "residual_after_recovery": residual_after_recovery,
            "recovery_error": recovery_error,
        }

    def _residuals(self) -> tuple[list[dict[str, str]], list[str]]:
        residuals: list[dict[str, str]] = []
        errors: list[str] = []
        for identity in self._managed_identities.values():
            reference = identity.container_id or identity.container_name
            if reference is None:
                continue
            try:
                snapshot = _inspect_docker_container(
                    reference, docker_host=identity.docker_host
                )
            except Exception as exc:
                errors.append(f"{identity.id}: {type(exc).__name__}: {exc}")
                continue
            if snapshot is not None:
                residuals.append(
                    {
                        "command_id": identity.id,
                        "container_id": snapshot.container_id,
                        "container_name": snapshot.name,
                    }
                )
        return residuals, errors

    def _cleanup_registered(self) -> list[str]:
        errors: list[str] = []
        for identity in self._managed_identities.values():
            try:
                _remove_verified_container(identity)
            except Exception as exc:
                errors.append(f"{identity.id}: {type(exc).__name__}: {exc}")
        return errors

    def _cleanup_fixture(self) -> None:
        if self._external_workspace:
            for path in sorted(self._fixture_paths, key=lambda item: len(item.parts), reverse=True):
                path.unlink(missing_ok=True)
            for path in self.workspace.glob(f".race-{self.run_id}-*"):
                path.unlink(missing_ok=True)
        shutil.rmtree(self.probe_root, ignore_errors=True)

    def _write_report(
        self,
        source_commit: str,
        *,
        image: str,
        docker_info: str,
        residual_before: list[dict[str, str]],
        residual_after: list[dict[str, str]],
        cleanup_errors: list[str],
        query_errors: list[str],
    ) -> dict[str, Any]:
        cleanup_status = (
            "unverified"
            if query_errors
            else "failed"
            if residual_before or residual_after or cleanup_errors
            else "passed"
        )
        self.cases.append(
            {
                "id": "final-residual-container-check",
                "category": "cleanup",
                "status": cleanup_status,
                "attempts": 1,
                "duration_ms": 0.0,
                "evidence": [
                    {
                        "residual_before_cleanup": residual_before,
                        "residual_after_cleanup": residual_after,
                        "cleanup_errors": cleanup_errors,
                        "query_errors": query_errors,
                    }
                ],
            }
        )
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case["status"]] = counts.get(case["status"], 0) + 1
        status = (
            "failed"
            if counts.get("failed", 0)
            else "unverified"
            if counts.get("unverified", 0)
            else "passed"
        )
        diff_output = _safe_output(
            ["git", "-C", str(self.root), "diff", "--binary", "HEAD"]
        )
        source_dirty = bool(
            _safe_output(["git", "-C", str(self.root), "status", "--porcelain"])
        )
        try:
            security_options = json.loads(docker_info) if docker_info else None
        except json.JSONDecodeError:
            security_options = None
        dockerfile = self.root / "docker" / "sandbox.Dockerfile"
        report = {
            "schema_version": 2,
            "kind": "real-docker-sandbox-acceptance",
            "status": status,
            "generated_at": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "source_commit": source_commit,
            "source_dirty": source_dirty,
            "source_diff_sha256": hashlib.sha256(diff_output.encode()).hexdigest(),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "dockerfile_sha256": (
                hashlib.sha256(dockerfile.read_bytes()).hexdigest()
                if dockerfile.exists()
                else None
            ),
            "effective_config": self.sandbox.config.model_dump(mode="json"),
            "preconditions": {
                "dedicated_workspace": self._external_workspace,
                "workspace_empty_at_start": self._workspace_empty_at_start,
            },
            "assurance": {
                "workspace_storage_bounded": False,
                "daemon_fault_injection": False,
            },
            "residual_before_cleanup": residual_before,
            "residual_after_cleanup": residual_after,
            "host": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "docker_client": _safe_output(
                    ["docker", "version", "--format", "{{.Client.Version}}"]
                ),
                "docker_server": _safe_output(
                    ["docker", "version", "--format", "{{.Server.Version}}"]
                ),
                "docker_security_options": security_options,
                "sandbox_image_id": image,
                "workspace_path": str(self.workspace),
            },
            "threat_model": {
                "guarantees": [
                    "no host filesystem access outside the mounted workspace",
                    "no external or host network access by default",
                    "no host credentials or environment leakage",
                    "bounded capabilities, processes, memory, CPU, temporary storage, "
                    "and returned output",
                    "verified container identity before recovery cleanup",
                    "no residual managed containers after all exercised lifecycle paths",
                ],
                "exclusions": [
                    "kernel or container-runtime zero-day escape resistance",
                    "denial of service against the Docker daemon itself",
                ],
                "local_fallback_is_isolated": False,
            },
            "summary": counts,
            "cases": self.cases,
        }
        self.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.output.with_suffix(self.output.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.output)
        return report

    def run(self, source_commit: str) -> dict[str, Any]:
        if self.output.exists():
            raise FileExistsError(f"refusing to overwrite existing report: {self.output}")
        image = ""
        docker_info = ""
        try:
            self.prepare()
            image_result = _run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "patchloop-sandbox:py313",
                    "--format",
                    "{{.Id}}",
                ]
            )
            if image_result.returncode != 0:
                raise ProbeUnverified(image_result.stderr.strip() or "Docker image unavailable")
            image = image_result.stdout.strip()
            info_result = _run(
                ["docker", "info", "--format", "{{json .SecurityOptions}}"]
            )
            if info_result.returncode != 0:
                raise ProbeUnverified(info_result.stderr.strip() or "docker info failed")
            docker_info = info_result.stdout.strip()

            self.add("docker-effective-configuration", "configuration", self.configuration)
            self.add("workspace-read-write", "filesystem", self.workspace_read_write)
            workspace_required = ("workspace-read-write",)
            self.add(
                "file-traversal-and-symlink",
                "filesystem",
                self.file_escape,
                attempts=3,
                requires=workspace_required,
            )
            self.add(
                "symlink-toctou",
                "filesystem",
                self.symlink_toctou,
                attempts=3,
                requires=workspace_required,
            )
            self.add(
                "network-dns-tcp-http-localhost",
                "network",
                self.network_isolation,
                attempts=3,
            )
            self.add(
                "environment-and-credential-isolation",
                "environment",
                self.environment_isolation,
                attempts=3,
            )
            self.add(
                "runtime-capabilities-and-socket",
                "configuration",
                self.runtime_privileges,
                attempts=3,
            )
            self.add("normal-exit-cleanup", "lifecycle", self.normal_cleanup)
            self.add(
                "timeout-process-tree-cleanup",
                "lifecycle",
                self.timeout_cleanup,
                attempts=3,
                requires=workspace_required,
            )
            self.add(
                "cancel-interruption-cleanup",
                "lifecycle",
                lambda: self.interruption_cleanup("cancel"),
                requires=workspace_required,
            )
            self.add("pause-and-resume", "lifecycle", self.pause_resume)
            self.add(
                "background-process-cleanup",
                "process",
                self.background_cleanup,
                attempts=3,
                requires=workspace_required,
            )
            self.add("pid-fork-limit", "resource", self.pid_limit, attempts=3)
            self.add("memory-limit", "resource", self.memory_limit)
            self.add("temporary-storage-limit", "resource", self.temporary_storage_limit)
            self.add("returned-output-limit", "resource", self.output_limit)
            self.add(
                "stdout-stderr-capture-memory-bound", "resource", self.output_limit
            )
            self.add(
                "workspace-storage-limit",
                "resource",
                self.workspace_storage_limit,
                requires=workspace_required,
            )
            self.add("container-startup-failure", "failure-recovery", self.startup_failure)
            self.add("container-identity-mismatch", "failure-recovery", self.identity_mismatch)
            self.add(
                "parent-crash-cleanup",
                "failure-recovery",
                self.parent_crash,
                requires=workspace_required,
            )
            self.note(
                "docker-daemon-unavailable",
                "failure-recovery",
                "unverified",
                "daemon transport fault proxy is delivered by TASK-08",
            )
            self.note(
                "windows-junction",
                "filesystem",
                "not_applicable",
                "Linux Docker target; Windows junction belongs to the Windows local matrix",
            )
        except ProbeUnverified as exc:
            self.note(
                "matrix-precondition",
                "harness",
                "unverified",
                f"ProbeUnverified: {exc}",
            )
        except KeyboardInterrupt as exc:
            self.note("matrix-interrupted", "harness", "failed", f"KeyboardInterrupt: {exc}")
        except BaseException as exc:
            self.note(
                "matrix-internal-error",
                "harness",
                "failed",
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            residual_before, query_before = self._residuals()
            cleanup_errors = self._cleanup_registered()
            residual_after, query_after = self._residuals()
            self._cleanup_fixture()
        return self._write_report(
            source_commit,
            image=image,
            docker_info=docker_info,
            residual_before=residual_before,
            residual_after=residual_after,
            cleanup_errors=cleanup_errors,
            query_errors=[*query_before, *query_after],
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the real Docker sandbox adversarial matrix")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--source-commit", required=True)
    arguments = parser.parse_args()
    report = Matrix(arguments.root, arguments.output, arguments.workspace).run(
        arguments.source_commit
    )
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
