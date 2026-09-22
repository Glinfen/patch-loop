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
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from patchloop.domain import Task
from patchloop.execution.ownership import ExecutionOwnershipManager, LeasePolicy
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import RecoveryRequired
from patchloop.sandbox import (
    DockerSandbox,
    DockerSandboxConfig,
    ManagedCommandIdentity,
    SandboxError,
    SandboxInterruptedError,
    SandboxTimeoutError,
    _inspect_docker_container,
    _local_process_start_marker,
    _remove_verified_container,
    reconcile_managed_command,
)

if __package__:
    from tests.acceptance.docker_fault_proxy import DockerFaultProxy
else:
    # The documented acceptance command executes this file directly, so the
    # sibling directory (rather than the repository root) is on sys.path.
    from docker_fault_proxy import DockerFaultProxy

CaseProbe = Callable[[], dict[str, Any]]


class ProbeUnverified(RuntimeError):
    pass


@dataclass(frozen=True)
class _SourceEvidence:
    commit: str
    dirty: bool
    diff_sha256: str


def _run_git(root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"could not inspect source repository: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
        raise ValueError(f"could not inspect source repository: {detail[:1000]}")
    return completed


def _source_evidence(root: Path, expected_commit: str) -> _SourceEvidence:
    """Bind an acceptance run to the actual Git HEAD and complete source delta."""

    actual_root = Path(_run_git(root, ["rev-parse", "--show-toplevel"]).stdout.strip()).resolve(
        strict=True
    )
    if actual_root != root.resolve(strict=True):
        raise ValueError("--root must exactly equal the Git worktree root")
    commit = _run_git(root, ["rev-parse", "HEAD"]).stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("git HEAD did not resolve to a full commit hash")
    if expected_commit != commit:
        raise ValueError(
            f"--source-commit does not match actual HEAD: expected {commit}, got {expected_commit}"
        )

    tracked = _run_git(
        root,
        [
            "diff",
            "--binary",
            "--no-ext-diff",
            "HEAD",
            "--",
            ".",
            ":(exclude)benchmarks/results",
        ],
    ).stdout
    untracked_raw = _run_git(
        root, ["ls-files", "--others", "--exclude-standard", "-z", "--", "."]
    ).stdout
    untracked = sorted(
        item
        for item in untracked_raw.split("\0")
        if item and not item.replace("\\", "/").startswith("benchmarks/results/")
    )
    digest = hashlib.sha256()
    digest.update(b"tracked-diff\0")
    digest.update(tracked.encode("utf-8"))
    for relative in untracked:
        candidate = root / relative
        digest.update(b"untracked\0")
        digest.update(relative.replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        if candidate.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(candidate).encode("utf-8"))
        elif candidate.is_file():
            digest.update(b"file\0")
            digest.update(hashlib.sha256(candidate.read_bytes()).digest())
        else:
            raise ValueError(f"untracked source entry is not a regular file: {relative}")
    return _SourceEvidence(
        commit=commit,
        dirty=bool(tracked or untracked),
        diff_sha256=digest.hexdigest(),
    )


def _validate_report(report: dict[str, Any]) -> None:
    """Validate schema, aggregate counts, gates, and source evidence before publish."""

    expected_keys = {
        "schema_version",
        "kind",
        "status",
        "generated_at",
        "run_id",
        "source_commit",
        "source_dirty",
        "source_diff_sha256",
        "harness_sha256",
        "dockerfile_sha256",
        "effective_config",
        "preconditions",
        "assurance",
        "residual_before_cleanup",
        "residual_after_cleanup",
        "host",
        "threat_model",
        "summary",
        "cases",
    }
    if set(report) != expected_keys:
        raise ValueError("real matrix report has unexpected or missing top-level fields")
    if report["schema_version"] != 2 or report["kind"] != "real-docker-sandbox-acceptance":
        raise ValueError("real matrix report identity is invalid")
    if re.fullmatch(r"[0-9a-f]{40}", report["source_commit"]) is None:
        raise ValueError("real matrix report source_commit is invalid")
    for field in ("source_diff_sha256", "harness_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", report[field]) is None:
            raise ValueError(f"real matrix report {field} is invalid")
    dockerfile_hash = report["dockerfile_sha256"]
    if dockerfile_hash is not None and re.fullmatch(r"[0-9a-f]{64}", dockerfile_hash) is None:
        raise ValueError("real matrix report dockerfile_sha256 is invalid")
    if not isinstance(report["source_dirty"], bool):
        raise ValueError("real matrix report source_dirty must be boolean")

    cases = report["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("real matrix report cases must be non-empty")
    allowed_statuses = {"passed", "failed", "unverified", "not_applicable"}
    identifiers: set[str] = set()
    for case in cases:
        required = {"id", "category", "status", "attempts", "duration_ms", "evidence"}
        if not isinstance(case, dict) or not required.issubset(case):
            raise ValueError("real matrix report case schema is invalid")
        if set(case) - (required | {"requires"}):
            raise ValueError(f"real matrix report case has unexpected fields: {case.get('id')}")
        if not isinstance(case["id"], str) or not case["id"] or case["id"] in identifiers:
            raise ValueError("real matrix report case IDs must be unique non-empty strings")
        identifiers.add(case["id"])
        if case["status"] not in allowed_statuses:
            raise ValueError(f"real matrix report case status is invalid: {case['id']}")
        if not isinstance(case["attempts"], int) or case["attempts"] < 0:
            raise ValueError(f"real matrix report attempts are invalid: {case['id']}")
        if not isinstance(case["evidence"], list) or not case["evidence"]:
            raise ValueError(f"real matrix report evidence is invalid: {case['id']}")

    counts = dict(Counter(case["status"] for case in cases))
    if report["summary"] != counts:
        raise ValueError("real matrix report summary is inconsistent with cases")
    expected_status = (
        "failed"
        if counts.get("failed", 0)
        else "unverified"
        if counts.get("unverified", 0)
        else "passed"
    )
    if report["status"] != expected_status:
        raise ValueError("real matrix report aggregate status is inconsistent")

    final = next((case for case in cases if case["id"] == "final-residual-container-check"), None)
    if final is None or len(final["evidence"]) != 1:
        raise ValueError("real matrix report is missing final cleanup evidence")
    cleanup = final["evidence"][0]
    if (
        cleanup.get("residual_before_cleanup") != report["residual_before_cleanup"]
        or cleanup.get("residual_after_cleanup") != report["residual_after_cleanup"]
    ):
        raise ValueError("real matrix report cleanup evidence is inconsistent")
    if report["status"] == "passed":
        if report["residual_before_cleanup"] or report["residual_after_cleanup"]:
            raise ValueError("passed real matrix report contains residual containers")
        repeated_cases = {
            "file-traversal-and-symlink",
            "symlink-toctou",
            "network-dns-tcp-http-localhost",
            "environment-and-credential-isolation",
            "runtime-capabilities-and-socket",
            "normal-exit-cleanup",
            "timeout-process-tree-cleanup",
            "cancel-interruption-cleanup",
            "pause-and-resume",
            "background-process-cleanup",
            "pid-fork-limit",
            "container-startup-failure",
            "container-identity-mismatch",
            "parent-crash-cleanup",
            "parent-crash-recovery",
            "docker-daemon-unavailable",
        }
        indexed = {case["id"]: case for case in cases}
        if not repeated_cases.issubset(indexed):
            raise ValueError("passed real matrix report is missing repeated race cases")
        for case_id in repeated_cases:
            case = indexed[case_id]
            if case["attempts"] != 3 or len(case["evidence"]) != 3:
                raise ValueError(f"passed real matrix report lacks three attempts: {case_id}")
        assurance = report["assurance"]
        config = report["effective_config"]
        if not assurance.get("daemon_fault_injection"):
            raise ValueError("passed real matrix report lacks daemon fault evidence")
        if config.get("workspace_limit_mb") is not None and not assurance.get(
            "workspace_storage_bounded"
        ):
            raise ValueError("passed real matrix report lacks workspace capacity evidence")


class _MatrixClock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def _docker_environment(docker_host: str | None) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("DOCKER_HOST", None)
    if docker_host is not None:
        environment["DOCKER_HOST"] = docker_host
    return environment


def _run(
    command: list[str], *, timeout: float = 30, docker_host: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        env=_docker_environment(docker_host),
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


def _safe_output(command: list[str], *, docker_host: str | None = None) -> str:
    try:
        completed = _run(command, docker_host=docker_host)
    except BaseException:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _container_exists(name: str, docker_host: str | None = None) -> bool:
    if docker_host is None:
        return _inspect_docker_container(name) is not None
    return _inspect_docker_container(name, docker_host=docker_host) is not None


def _labelled_containers(execution_id: str, docker_host: str | None = None) -> list[str]:
    completed = _run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=patchloop.execution_id={execution_id}",
            "--format",
            "{{.Names}}",
        ],
        docker_host=docker_host,
    )
    if completed.returncode != 0:
        raise ProbeUnverified(completed.stderr.strip() or "docker ps query failed")
    return [line for line in completed.stdout.splitlines() if line.strip()]


class Matrix:
    def __init__(
        self,
        root: Path,
        output: Path,
        workspace: Path | None = None,
        *,
        workspace_limit_mb: int | None = None,
        workspace_inode_limit: int = 65_536,
        docker_host: str | None = None,
        daemon_fault_mode: str = "skip",
    ) -> None:
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
        self._requested_docker_host = docker_host
        self._upstream_docker_host = docker_host
        self.docker_host = docker_host
        self.daemon_fault_mode = daemon_fault_mode
        self._fault_proxy: DockerFaultProxy | None = None
        self.sandbox_config = DockerSandboxConfig(
            workspace_limit_mb=workspace_limit_mb,
            workspace_inode_limit=workspace_inode_limit,
        )
        self.sandbox = DockerSandbox(self.sandbox_config, docker_host=docker_host)
        self.cases: list[dict[str, Any]] = []
        self._managed_identities: dict[str, ManagedCommandIdentity] = {}
        self._fixture_paths: set[Path] = set()
        self._workspace_empty_at_start = False
        self.execution_id = f"real-matrix-{self.run_id}"
        self._bind_sandbox()

    def _bind_sandbox(self) -> None:
        self.sandbox.bind_execution(
            self.execution_id,
            command_started=self._record_identity,
            command_finished=lambda identity: None,
            interruption_probe=lambda: None,
        )

    def _start_fault_proxy(self) -> None:
        if self.daemon_fault_mode != "proxy":
            return
        if os.name != "posix":
            raise ProbeUnverified("Docker fault proxy requires a POSIX Unix socket host")
        raw = self._requested_docker_host or "unix:///var/run/docker.sock"
        if not raw.startswith("unix:///"):
            raise ProbeUnverified("--docker-host must name an absolute local Unix socket")
        upstream = Path(raw.removeprefix("unix://"))
        # Linux limits AF_UNIX paths to roughly 108 bytes.  A checkout path plus
        # the run-scoped probe directory can exceed that before the matrix starts.
        control_dir = Path(tempfile.gettempdir()).resolve() / f"patchloop-dfp-{self.run_id[:16]}"
        proxy = DockerFaultProxy(upstream, control_dir)
        self._fault_proxy = proxy
        self.docker_host = proxy.start()
        self._upstream_docker_host = raw
        self.sandbox = DockerSandbox(self.sandbox_config, docker_host=self.docker_host)
        self._bind_sandbox()

    def _run_docker(
        self, command: list[str], *, timeout: float = 30, upstream: bool = False
    ) -> subprocess.CompletedProcess[str]:
        host = self._upstream_docker_host if upstream else self.docker_host
        return _run(command, timeout=timeout, docker_host=host)

    def _container_exists(self, reference: str, *, upstream: bool = False) -> bool:
        host = self._upstream_docker_host if upstream else self.docker_host
        return _container_exists(reference, host)

    def _new_sandbox(self, **updates: Any) -> DockerSandbox:
        config = self.sandbox_config.model_copy(update=updates)
        return DockerSandbox(config, docker_host=self.docker_host)

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
        resolved_image = self.sandbox._resolve_image_id()
        create, name = self.sandbox.build_create_command(
            ["python", "-c", "print('configured')"],
            self.workspace,
            command_id=command_id,
            execution_id=self.execution_id,
            image=resolved_image,
        )
        created = self._run_docker(create)
        if created.returncode != 0:
            return {"passed": False, "create_error": created.stderr.strip()}
        snapshot = _inspect_docker_container(name, docker_host=self.docker_host)
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
            docker_host=self.docker_host,
        )
        self._record_identity(identity)
        try:
            inspected = json.loads(self._run_docker(["docker", "inspect", name]).stdout)[0]
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
                "memory_swap_disabled": host.get("MemorySwap") == 512 * 1024 * 1024,
                "pid_limit": host.get("PidsLimit") == 128,
                "log_driver_none": (host.get("LogConfig") or {}).get("Type") == "none",
                "image_id_pinned": inspected.get("Image") == resolved_image
                and config.get("Image") == resolved_image,
                "workspace_user": config.get("User") == self.sandbox._effective_user(),
                "tmpfs_limit": "/tmp" in tmpfs and "size=64m" in tmpfs["/tmp"],
                "tmpfs_hardened": "/tmp" in tmpfs
                and "noexec" in tmpfs["/tmp"]
                and "nosuid" in tmpfs["/tmp"]
                and "nodev" in tmpfs["/tmp"],
                "identity_labels": config.get("Labels", {}).get("patchloop.command_id")
                == command_id
                and config.get("Labels", {}).get("patchloop.execution_id") == self.execution_id,
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
def accessible(path):
    try:
        return Path(path).exists()
    except PermissionError:
        return False
print(json.dumps({{
    'explicit_variable': os.environ.get({variable!r}),
    'sentinel_in_environment': needle in environment or needle in proc_environment,
    'credential_paths_present': [path for path in paths if accessible(path)],
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
        target_stat = target.stat() if target.exists() else None
        expected_uid = os.getuid() if hasattr(os, "getuid") else None
        expected_gid = os.getgid() if hasattr(os, "getgid") else None
        identity_matches = target_stat is not None and (
            expected_uid is None
            or (target_stat.st_uid == expected_uid and target_stat.st_gid == expected_gid)
        )
        target.unlink(missing_ok=True)
        return {
            "passed": result.exit_code == 0 and content == "SAFE" and identity_matches,
            "exit_code": result.exit_code,
            "host_file_content": content,
            "expected_uid": expected_uid,
            "expected_gid": expected_gid,
            "file_uid": None if target_stat is None else target_stat.st_uid,
            "file_gid": None if target_stat is None else target_stat.st_gid,
            "output_tail": result.output[-500:],
        }

    def normal_cleanup(self) -> dict[str, Any]:
        started: list[ManagedCommandIdentity] = []
        finished: list[ManagedCommandIdentity] = []
        sandbox = self._new_sandbox()
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
        workloads = [item for item in started if item.purpose == "workload"]
        finished_workloads = [item for item in finished if item.purpose == "workload"]
        name = workloads[0].container_name or ""
        return {
            "passed": (
                result.exit_code == 0
                and len(workloads) == len(finished_workloads) == 1
                and not self._container_exists(name)
            ),
            "container": name,
            "finished_status": finished_workloads[0].status.value,
            "container_absent": not self._container_exists(name),
        }

    def timeout_cleanup(self) -> dict[str, Any]:
        started: list[ManagedCommandIdentity] = []
        finished: list[ManagedCommandIdentity] = []
        sandbox = self._new_sandbox()
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
        workloads = [item for item in started if item.purpose == "workload"]
        finished_workloads = [item for item in finished if item.purpose == "workload"]
        name = workloads[0].container_name or ""
        return {
            "passed": (
                timed_out
                and len(workloads) == len(finished_workloads) == 1
                and not self._container_exists(name)
            ),
            "timeout_raised": timed_out,
            "container": name,
            "container_absent": not self._container_exists(name),
            "cleanup_reason": (
                finished_workloads[0].cleanup_reason if finished_workloads else None
            ),
        }

    def interruption_cleanup(self, reason: str) -> dict[str, Any]:
        started: list[ManagedCommandIdentity] = []
        finished: list[ManagedCommandIdentity] = []
        sandbox = self._new_sandbox()
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
            "passed": interrupted and len(finished) == 1 and not self._container_exists(name),
            "interrupted": interrupted,
            "container_absent": not self._container_exists(name),
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
        sandbox = self._new_sandbox(memory_mb=128)
        control = self.execute_python(
            "x=bytearray(32*1024*1024); "
            "[(x.__setitem__(index,1)) for index in range(0,len(x),4096)]; print(sum(x))",
            timeout=15,
            sandbox=sandbox,
        )
        result = self.execute_python(
            "x=bytearray(256*1024*1024); "
            "[(x.__setitem__(index,1)) for index in range(0,len(x),4096)]; print(sum(x))",
            timeout=20,
            sandbox=sandbox,
        )
        return {
            "passed": control.exit_code == 0
            and not control.oom_killed
            and result.exit_code != 0
            and result.oom_killed,
            "control_exit_code": control.exit_code,
            "control_oom_killed": control.oom_killed,
            "exit_code": result.exit_code,
            "oom_killed": result.oom_killed,
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
            "for _ in range(4)]",
            timeout=30,
            max_output=4096,
        )
        return {
            "passed": result.exit_code == 0
            and len(result.output) <= 4096
            and result.output_truncated
            and result.stdout_bytes == 4 * 1024 * 1024
            and result.stderr_bytes == 4 * 1024 * 1024,
            "returned_chars": len(result.output),
            "stdout_bytes": result.stdout_bytes,
            "stderr_bytes": result.stderr_bytes,
            "output_truncated": result.output_truncated,
        }

    def output_capture_memory_bound(self) -> dict[str, Any]:
        if not Path("/proc/self/status").is_file():
            raise ProbeUnverified("RSS sampling requires Linux /proc")
        token = uuid4().hex
        ready = self.probe_root / f"output-{token}.ready"
        go = self.probe_root / f"output-{token}.go"
        stop = self.probe_root / f"output-{token}.stop"
        result_path = self.probe_root / f"output-{token}.json"
        sample_path = self.probe_root / f"output-{token}.rss.json"
        config_path = self.probe_root / f"output-{token}.config.json"
        config_path.write_text(self.sandbox_config.model_dump_json(), encoding="utf-8")
        worker_code = r"""
import json, sys, time
from pathlib import Path
from patchloop.sandbox import DockerSandbox, DockerSandboxConfig
workspace, ready, go, result_path, config_path, docker_host = sys.argv[1:]
Path(ready).write_text('ready')
deadline = time.monotonic() + 15
while not Path(go).exists() and time.monotonic() < deadline:
    time.sleep(.01)
if not Path(go).exists():
    raise SystemExit(3)
sandbox = DockerSandbox(
    DockerSandboxConfig.model_validate_json(Path(config_path).read_text()),
    docker_host=docker_host or None,
)
sandbox.bind_execution(
    'output-memory-worker',
    command_started=lambda identity: None,
    command_finished=lambda identity: None,
    interruption_probe=lambda: None,
)
result = sandbox.execute(
    [
        'python',
        '-c',
        "import os; [(os.write(1,b'O'*(1024*1024)),"
        "os.write(2,b'E'*(1024*1024))) for _ in range(128)]",
    ],
    Path(workspace), timeout_seconds=90, max_output_chars=4096,
)
Path(result_path).write_text(json.dumps({
    'exit_code': result.exit_code,
    'returned_chars': len(result.output),
    'truncated': result.output_truncated,
    'stdout_bytes': result.stdout_bytes,
    'stderr_bytes': result.stderr_bytes,
}))
"""
        sampler_code = r"""
import json, sys, time
from pathlib import Path
root = int(sys.argv[1]); stop = Path(sys.argv[2]); output = Path(sys.argv[3])
def children(pid):
    try: raw = Path(f'/proc/{pid}/task/{pid}/children').read_text()
    except OSError: return []
    return [int(value) for value in raw.split()]
def tree(pid):
    seen=set(); pending=[pid]
    while pending:
        current=pending.pop()
        if current in seen: continue
        seen.add(current); pending.extend(children(current))
    return seen
def rss(pid):
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('VmRSS:'): return int(line.split()[1])*1024
    except OSError: pass
    return 0
baseline=sum(rss(pid) for pid in tree(root)); peak=baseline; samples=0
deadline=time.monotonic()+120
while not stop.exists() and time.monotonic()<deadline:
    peak=max(peak,sum(rss(pid) for pid in tree(root))); samples+=1; time.sleep(.02)
output.write_text(json.dumps({'baseline_bytes':baseline,'peak_bytes':peak,'delta_bytes':peak-baseline,'samples':samples}))
"""
        worker = subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker_code,
                str(self.workspace),
                str(ready),
                str(go),
                str(result_path),
                str(config_path),
                self.docker_host or "",
            ],
            cwd=self.root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        sampler: subprocess.Popen[bytes] | None = None
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and worker.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if not ready.exists():
                raise ProbeUnverified("output worker did not reach the sampling barrier")
            sampler = subprocess.Popen(
                [sys.executable, "-c", sampler_code, str(worker.pid), str(stop), str(sample_path)],
                cwd=self.root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.1)
            go.touch()
            worker.wait(timeout=120)
        finally:
            stop.touch()
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)
            if sampler is not None:
                sampler.wait(timeout=10)
        if worker.returncode != 0 or not result_path.is_file():
            raise ProbeUnverified(f"output worker failed with exit code {worker.returncode}")
        if sampler is None or sampler.returncode != 0 or not sample_path.is_file():
            raise ProbeUnverified("independent RSS sampler failed")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        sample = json.loads(sample_path.read_text(encoding="utf-8"))
        bound = 96 * 1024 * 1024
        return {
            "passed": result["exit_code"] == 0
            and result["truncated"]
            and result["returned_chars"] <= 4096
            and result["stdout_bytes"] == 128 * 1024 * 1024
            and result["stderr_bytes"] == 128 * 1024 * 1024
            and sample["samples"] > 0
            and sample["delta_bytes"] <= bound,
            "traffic_bytes": result["stdout_bytes"] + result["stderr_bytes"],
            "retained_chars": result["returned_chars"],
            "rss": sample,
            "rss_delta_bound_bytes": bound,
            "sampler_pid": sampler.pid,
            "worker_pid": worker.pid,
        }

    def workspace_storage_limit(self) -> dict[str, Any]:
        target = self._track_fixture(self.workspace / "workspace-fill.bin")
        sparse = self._track_fixture(self.workspace / "workspace-sparse.bin")
        inode_dir = self._track_fixture(self.workspace / "workspace-inodes")
        recovery = self._track_fixture(self.workspace / "workspace-recovered.txt")
        for path in (target, sparse, recovery):
            path.unlink(missing_ok=True)
        shutil.rmtree(inode_dir, ignore_errors=True)
        code = f"""
import errno, json, os, pathlib, shutil
root = pathlib.Path('/workspace')
target = root / 'workspace-fill.bin'
sparse = root / 'workspace-sparse.bin'
inode_dir = root / 'workspace-inodes'
written = 0
byte_errno = None
with sparse.open('wb') as stream:
    stream.truncate(96 * 1024 * 1024)
sparse_logical = sparse.stat().st_size
sparse_allocated = sparse.stat().st_blocks * 512
try:
    with target.open('wb') as stream:
        for _ in range(96):
            stream.write(b'x' * 1024 * 1024)
            stream.flush()
            os.fsync(stream.fileno())
            written += 1024 * 1024
except OSError as exc:
    byte_errno = exc.errno
target.unlink(missing_ok=True)
sparse.unlink(missing_ok=True)
inode_dir.mkdir()
inodes_created = 0
inode_errno = None
try:
    for index in range({min(self.sandbox_config.workspace_inode_limit + 1024, 100_000)}):
        (inode_dir / str(index)).touch(exist_ok=False)
        inodes_created += 1
except OSError as exc:
    inode_errno = exc.errno
shutil.rmtree(inode_dir)
(root / 'workspace-recovered.txt').write_text('recovered')
print(json.dumps({{
    'written': written, 'byte_errno': byte_errno,
    'sparse_logical': sparse_logical, 'sparse_allocated': sparse_allocated,
    'inodes_created': inodes_created, 'inode_errno': inode_errno,
}}))
"""
        result = self.execute_python(code, timeout=60)
        data = _json_output(result.output)
        original_intact = (self.workspace / "safe.txt").read_text(encoding="utf-8") == "SAFE"
        recovered = recovery.read_text(encoding="utf-8") if recovery.exists() else None
        recovery.unlink(missing_ok=True)
        return {
            "passed": result.exit_code == 0
            and data["written"] > 0
            and data["written"] < 96 * 1024 * 1024
            and data["byte_errno"] == 28
            and data["sparse_logical"] == 96 * 1024 * 1024
            and data["sparse_allocated"] < data["sparse_logical"]
            and data["inodes_created"] > 0
            and data["inode_errno"] == 28
            and original_intact
            and recovered == "recovered",
            "exit_code": result.exit_code,
            "reported_bytes_written": data["written"],
            "byte_errno": data["byte_errno"],
            "sparse_logical_bytes": data["sparse_logical"],
            "sparse_allocated_bytes": data["sparse_allocated"],
            "inodes_created": data["inodes_created"],
            "inode_errno": data["inode_errno"],
            "fixture_intact": original_intact,
            "post_enospc_write": recovered,
        }

    def startup_failure(self) -> dict[str, Any]:
        sandbox = self._new_sandbox()
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
            "passed": denied and not _labelled_containers(execution_id, self.docker_host),
            "structured_exception": denied,
            "detail": detail,
            "residual_containers": _labelled_containers(execution_id, self.docker_host),
        }

    def identity_mismatch(self) -> dict[str, Any]:
        command_id = uuid4().hex
        name = f"patchloop-{command_id}"
        create = self._run_docker(
            [
                "docker",
                "create",
                "--name",
                name,
                "--label",
                "patchloop.command_id=wrong-identity",
                "--label",
                f"patchloop.execution_id=matrix-mismatch-{self.run_id}",
                self.sandbox.config.image,
                "python",
                "-c",
                "print('identity')",
            ]
        )
        if create.returncode != 0:
            return {"passed": False, "create_error": create.stderr.strip()}
        snapshot = _inspect_docker_container(name, docker_host=self.docker_host)
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
            docker_host=self.docker_host,
        )
        self._record_identity(cleanup_identity)
        identity = ManagedCommandIdentity(
            id=command_id,
            execution_id=f"matrix-mismatch-{self.run_id}",
            backend="docker",
            process_id=os.getpid(),
            process_start_marker="matrix",
            container_name=name,
            docker_host=self.docker_host,
        )
        refused = False
        detail = ""
        try:
            reconcile_managed_command(identity)
        except Exception as exc:
            refused = True
            detail = str(exc)
        still_present = self._container_exists(name)
        _remove_verified_container(cleanup_identity)
        return {
            "passed": refused and still_present,
            "cleanup_refused": refused,
            "container_preserved_until_explicit_cleanup": still_present,
            "detail": detail,
        }

    def _parent_crash_evidence(self) -> dict[str, Any]:
        identity_path = self.probe_root / "parent-crash-identity.json"
        started_path = self._track_fixture(self.workspace / "parent-crash-started.txt")
        identity_path.unlink(missing_ok=True)
        started_path.unlink(missing_ok=True)
        child_code = f"""
from pathlib import Path
from patchloop.sandbox import DockerSandbox, DockerSandboxConfig
sandbox = DockerSandbox(
    DockerSandboxConfig.model_validate_json({self.sandbox_config.model_dump_json()!r}),
    docker_host={self.docker_host!r},
)
def started(identity):
    Path({str(identity_path)!r}).write_text(identity.model_dump_json())
sandbox.bind_execution(
    'matrix-parent-crash',
    command_started=started,
    command_finished=lambda identity: None,
    interruption_probe=lambda: None,
)
sandbox.execute(
    [
        'python',
        '-c',
        "from pathlib import Path; import time; "
        "Path('/workspace/parent-crash-started.txt').write_text('started'); "
        "time.sleep(60)",
    ],
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
                if (
                    identity.container_name
                    and self._container_exists(identity.container_name)
                    and started_path.exists()
                ):
                    self._record_identity(identity)
                    break
            time.sleep(0.05)
        if identity is None or identity.container_name is None:
            process.kill()
            process.wait(timeout=5)
            return {
                "automatic_passed": False,
                "recovery_passed": False,
                "detail": "managed container never became observable and started",
            }
        process.kill()
        process.wait(timeout=5)
        cleanup_started = time.monotonic()
        time.sleep(0.5)
        present_at_500ms = self._container_exists(identity.container_name)
        supervisor_at_500ms = (
            _local_process_start_marker(identity.process_id) == identity.process_start_marker
        )
        cleanup_deadline = cleanup_started + 20
        while time.monotonic() < cleanup_deadline:
            container_present = self._container_exists(identity.container_name)
            supervisor_present = (
                _local_process_start_marker(identity.process_id) == identity.process_start_marker
            )
            if not container_present and not supervisor_present:
                break
            time.sleep(0.1)
        residual_after_crash = self._container_exists(identity.container_name)
        supervisor_after_crash = (
            _local_process_start_marker(identity.process_id) == identity.process_start_marker
        )
        cleanup_elapsed_seconds = time.monotonic() - cleanup_started
        recovery_error = ""
        try:
            reconcile_managed_command(identity)
        except Exception as exc:
            recovery_error = f"{type(exc).__name__}: {exc}"
        residual_after_recovery = self._container_exists(identity.container_name)
        supervisor_after_recovery = (
            _local_process_start_marker(identity.process_id) == identity.process_start_marker
        )
        if residual_after_recovery:
            self._run_docker(["docker", "rm", "--force", identity.container_name])
        return {
            "automatic_passed": not residual_after_crash and not supervisor_after_crash,
            "recovery_passed": (
                not residual_after_recovery and not supervisor_after_recovery and not recovery_error
            ),
            "container": identity.container_name,
            "workload_started": started_path.exists(),
            "container_present_at_500ms": present_at_500ms,
            "supervisor_present_at_500ms": supervisor_at_500ms,
            "residual_after_parent_crash": residual_after_crash,
            "supervisor_after_parent_crash": supervisor_after_crash,
            "cleanup_elapsed_seconds": round(cleanup_elapsed_seconds, 3),
            "residual_after_recovery": residual_after_recovery,
            "supervisor_after_recovery": supervisor_after_recovery,
            "recovery_error": recovery_error,
        }

    def parent_crash_cleanup(self) -> dict[str, Any]:
        evidence = dict(self._parent_crash_evidence())
        return {"passed": bool(evidence.pop("automatic_passed")), **evidence}

    def parent_crash_recovery(self) -> dict[str, Any]:
        evidence = dict(self._parent_crash_evidence())
        return {"passed": bool(evidence.pop("recovery_passed")), **evidence}

    def docker_daemon_unavailable(self) -> dict[str, Any]:
        proxy = self._fault_proxy
        if proxy is None:
            raise ProbeUnverified("daemon fault mode is skip")
        attempt_id = uuid4().hex

        startup_error = ""
        proxy.cut_connections()
        try:
            unavailable = self._new_sandbox()
            unavailable.bind_execution(
                f"matrix-daemon-startup-{attempt_id}",
                command_started=self._record_identity,
                command_finished=lambda identity: None,
                interruption_probe=lambda: None,
            )
            try:
                unavailable.execute(
                    ["python", "-c", "print('must not start')"],
                    self.workspace,
                    timeout_seconds=10,
                    max_output_chars=1000,
                )
            except BaseException as exc:
                startup_error = f"{type(exc).__name__}: {exc}"
        finally:
            proxy.restore()

        other_command_id = uuid4().hex
        other_execution_id = f"matrix-other-task-{attempt_id}"
        other_create, other_name = self.sandbox.build_create_command(
            ["python", "-c", "print('other-task')"],
            self.workspace,
            command_id=other_command_id,
            execution_id=other_execution_id,
            image=self.sandbox._resolve_image_id(),
        )
        other_created = self._run_docker(other_create)
        if other_created.returncode != 0:
            raise ProbeUnverified(
                f"other-task control container create failed: {other_created.stderr}"
            )
        other_snapshot = _inspect_docker_container(other_name, docker_host=self.docker_host)
        if other_snapshot is None:
            raise ProbeUnverified("other-task control container was not inspectable")
        other_identity = ManagedCommandIdentity(
            id=other_command_id,
            execution_id=other_execution_id,
            backend="docker",
            process_id=os.getpid(),
            process_start_marker="acceptance-harness",
            container_name=other_name,
            container_id=other_snapshot.container_id,
            docker_host=self.docker_host,
        )
        self._record_identity(other_identity)

        store = SQLiteStore(self.probe_root / f"daemon-fault-state-{attempt_id}.db")
        first_task = store.prepare_task_execution(
            Task(
                id=f"daemon-fault-owner-{attempt_id}",
                goal="Hold the old Docker workspace writer",
                repository=str(self.workspace),
            )
        )
        second_task = store.prepare_task_execution(
            Task(
                id=f"daemon-fault-contender-{attempt_id}",
                goal="Acquire only after verified Docker cleanup",
                repository=str(self.workspace),
            )
        )
        clock = _MatrixClock()

        def manager(execution_id: str) -> ExecutionOwnershipManager:
            return ExecutionOwnershipManager(
                store,
                clock=clock,
                id_factory=lambda: execution_id,
                policy=LeasePolicy(
                    ttl=timedelta(seconds=60),
                    heartbeat_interval=timedelta(seconds=20),
                ),
            )

        first = manager(f"daemon-fault-execution-1-{attempt_id}").acquire(
            session_id=first_task.session_id or "",
            task_id=first_task.id,
            owner_id="daemon-fault-old-writer",
            repository=self.workspace,
            expected_version=first_task.version,
            workspace_writer=True,
        )

        marker = self._track_fixture(self.workspace / "daemon-fault-started.txt")
        marker.unlink(missing_ok=True)
        started: list[ManagedCommandIdentity] = []
        finished: list[ManagedCommandIdentity] = []
        errors: list[str] = []
        running = self._new_sandbox()
        running.bind_execution(
            first.execution.id,
            command_started=lambda identity: (
                store.register_managed_command(identity, lease_guard=first.lease_guard),
                started.append(identity),
                self._record_identity(identity),
            ),
            command_finished=lambda identity: (
                store.finish_managed_command(identity),
                finished.append(identity),
            ),
            interruption_probe=lambda: None,
        )

        def execute() -> None:
            try:
                running.execute(
                    [
                        "python",
                        "-c",
                        "from pathlib import Path; import time; "
                        "Path('/workspace/daemon-fault-started.txt').write_text('started'); "
                        "time.sleep(60)",
                    ],
                    self.workspace,
                    timeout_seconds=90,
                    max_output_chars=1000,
                )
            except BaseException as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        worker = threading.Thread(target=execute, name="daemon-fault-workload")
        worker.start()
        reconcile_while_cut = ""
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(0.05)
            workloads = [item for item in started if item.purpose == "workload"]
            if not marker.exists() or not workloads:
                raise ProbeUnverified("fault workload did not reach its positive control")
            identity = workloads[0]
            proxy.cut_connections()
            independent = self._run_docker(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                upstream=True,
            )
            worker.join(timeout=50)
            if worker.is_alive():
                raise ProbeUnverified("cleanup failure did not settle while proxy was cut")
            cleanup_confirmed_while_cut = any(
                item.purpose == "workload" and item.status.value in {"exited", "terminated"}
                for item in finished
            )
            clock.now += timedelta(seconds=61)
            blocked_execution_id = f"daemon-fault-execution-2-{attempt_id}"
            recovery_required = False
            try:
                manager(blocked_execution_id).acquire(
                    session_id=second_task.session_id or "",
                    task_id=second_task.id,
                    owner_id="daemon-fault-blocked-writer",
                    repository=self.workspace,
                    expected_version=store.get_task(second_task.id).version,
                    workspace_writer=True,
                )
            except RecoveryRequired as exc:
                recovery_required = True
                reconcile_while_cut = f"{type(exc).__name__}: {exc}"
            except BaseException as exc:
                reconcile_while_cut = f"{type(exc).__name__}: {exc}"
            blocked_execution = store.get_execution(blocked_execution_id)
            writer_blocked = recovery_required and blocked_execution.status.value == "released"
            proxy.restore()
            recovered_manager = manager(f"daemon-fault-execution-3-{attempt_id}")
            recovered_ownership = recovered_manager.acquire(
                session_id=second_task.session_id or "",
                task_id=second_task.id,
                owner_id="daemon-fault-recovered-writer",
                repository=self.workspace,
                expected_version=store.get_task(second_task.id).version,
                workspace_writer=True,
            )
            writer_granted_after_restore = recovered_ownership.workspace_lease is not None
            recovered_manager.release(recovered_ownership)
            absent_after_restore = not self._container_exists(identity.container_id or "")

            rm_command_id = uuid4().hex
            rm_execution_id = f"matrix-daemon-rm-{attempt_id}"
            create, rm_name = self.sandbox.build_create_command(
                ["python", "-c", "print('rm-fault')"],
                self.workspace,
                command_id=rm_command_id,
                execution_id=rm_execution_id,
                image=self.sandbox._resolve_image_id(),
            )
            created = self._run_docker(create)
            if created.returncode != 0:
                raise ProbeUnverified(f"rm-fault container create failed: {created.stderr}")
            rm_snapshot = _inspect_docker_container(rm_name, docker_host=self.docker_host)
            if rm_snapshot is None:
                raise ProbeUnverified("rm-fault container was not inspectable")
            rm_identity = ManagedCommandIdentity(
                id=rm_command_id,
                execution_id=rm_execution_id,
                backend="docker",
                process_id=os.getpid(),
                process_start_marker="acceptance-harness",
                container_name=rm_name,
                container_id=rm_snapshot.container_id,
                docker_host=self.docker_host,
            )
            self._record_identity(rm_identity)
            proxy.cut_after_connections(1)
            rm_disconnect_error = ""
            try:
                reconcile_managed_command(rm_identity)
            except BaseException as exc:
                rm_disconnect_error = f"{type(exc).__name__}: {exc}"
            cut_deadline = time.monotonic() + 2
            while proxy.socket_path.exists() and time.monotonic() < cut_deadline:
                time.sleep(0.02)
            rm_present_while_cut = self._container_exists(
                rm_identity.container_id or "", upstream=True
            )
            proxy.restore()
            reconcile_managed_command(rm_identity)
            rm_absent_after_restore = not self._container_exists(rm_identity.container_id or "")
            other_task_preserved = self._container_exists(other_identity.container_id or "")
            other_task_removed_after_probe = _remove_verified_container(other_identity)
            other_task_absent_after_probe = not self._container_exists(
                other_identity.container_id or ""
            )
            return {
                "passed": bool(startup_error)
                and marker.exists()
                and independent.returncode == 0
                and bool(errors)
                and not cleanup_confirmed_while_cut
                and bool(reconcile_while_cut)
                and writer_blocked
                and writer_granted_after_restore
                and absent_after_restore
                and bool(rm_disconnect_error)
                and rm_present_while_cut
                and rm_absent_after_restore
                and other_task_removed_after_probe
                and other_task_absent_after_probe
                and other_task_preserved,
                "fault_type": "client_transport_unavailable",
                "startup_error": startup_error,
                "workload_started": marker.exists(),
                "worker_errors": errors,
                "cleanup_confirmed_while_cut": cleanup_confirmed_while_cut,
                "reconcile_while_cut": reconcile_while_cut,
                "new_writer_blocked_while_cut": writer_blocked,
                "writer_granted_after_restore": writer_granted_after_restore,
                "independent_daemon_connection_ok": independent.returncode == 0,
                "absent_after_restore": absent_after_restore,
                "rm_disconnect_error": rm_disconnect_error,
                "rm_present_while_cut": rm_present_while_cut,
                "rm_absent_after_restore": rm_absent_after_restore,
                "other_task_container_preserved": other_task_preserved,
                "other_task_removed_after_probe": other_task_removed_after_probe,
                "other_task_absent_after_probe": other_task_absent_after_probe,
            }
        finally:
            with suppress(RuntimeError):
                proxy.restore()
            if worker.is_alive():
                running.terminate_all("fault-probe-finally")
                worker.join(timeout=10)

    def _residuals(self) -> tuple[list[dict[str, str]], list[str]]:
        residuals: list[dict[str, str]] = []
        errors: list[str] = []
        for identity in self._managed_identities.values():
            reference = identity.container_id or identity.container_name
            if reference is None:
                continue
            try:
                snapshot = _inspect_docker_container(reference, docker_host=identity.docker_host)
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
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            for path in self.workspace.glob(f".race-{self.run_id}-*"):
                path.unlink(missing_ok=True)
        shutil.rmtree(self.probe_root, ignore_errors=True)

    def _write_report(
        self,
        source: _SourceEvidence,
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
            "source_commit": source.commit,
            "source_dirty": source.dirty,
            "source_diff_sha256": source.diff_sha256,
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "dockerfile_sha256": (
                hashlib.sha256(dockerfile.read_bytes()).hexdigest() if dockerfile.exists() else None
            ),
            "effective_config": self.sandbox.config.model_dump(mode="json"),
            "preconditions": {
                "dedicated_workspace": self._external_workspace,
                "workspace_empty_at_start": self._workspace_empty_at_start,
                "workspace_capacity_evidence": (
                    None
                    if self.sandbox._workspace_capacity_evidence is None
                    else self.sandbox._workspace_capacity_evidence.model_dump(mode="json")
                ),
            },
            "assurance": {
                "workspace_storage_bounded": any(
                    item["id"] == "workspace-storage-limit" and item["status"] == "passed"
                    for item in self.cases
                ),
                "daemon_fault_injection": any(
                    item["id"] == "docker-daemon-unavailable" and item["status"] == "passed"
                    for item in self.cases
                ),
            },
            "residual_before_cleanup": residual_before,
            "residual_after_cleanup": residual_after,
            "host": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "docker_client": _safe_output(
                    ["docker", "version", "--format", "{{.Client.Version}}"],
                    docker_host=self._upstream_docker_host,
                ),
                "docker_server": _safe_output(
                    ["docker", "version", "--format", "{{.Server.Version}}"],
                    docker_host=self._upstream_docker_host,
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
                ]
                + (
                    ["workspace bytes and inodes are bounded by a verified dedicated ext4 mount"]
                    if self.sandbox.config.workspace_limit_mb is not None
                    else []
                ),
                "exclusions": [
                    "kernel or container-runtime zero-day escape resistance",
                    "denial of service against the Docker daemon itself",
                ],
                "local_fallback_is_isolated": False,
            },
            "summary": counts,
            "cases": self.cases,
        }
        _validate_report(report)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.output.with_suffix(self.output.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.output)
        return report

    def run(self, source_commit: str) -> dict[str, Any]:
        if self.output.exists():
            raise FileExistsError(f"refusing to overwrite existing report: {self.output}")
        source_before = _source_evidence(self.root, source_commit)
        image = ""
        docker_info = ""
        try:
            self.prepare()
            self._start_fault_proxy()
            image_result = self._run_docker(
                [
                    "docker",
                    "image",
                    "inspect",
                    self.sandbox.config.image,
                    "--format",
                    "{{.Id}}",
                ]
            )
            if image_result.returncode != 0:
                raise ProbeUnverified(image_result.stderr.strip() or "Docker image unavailable")
            image = image_result.stdout.strip()
            info_result = self._run_docker(
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
            self.add("normal-exit-cleanup", "lifecycle", self.normal_cleanup, attempts=3)
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
                attempts=3,
                requires=workspace_required,
            )
            self.add("pause-and-resume", "lifecycle", self.pause_resume, attempts=3)
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
                "stdout-stderr-capture-memory-bound",
                "resource",
                self.output_capture_memory_bound,
            )
            if self.sandbox.config.workspace_limit_mb is None:
                self.note(
                    "workspace-storage-limit",
                    "resource",
                    "unverified",
                    "workspace capacity enforcement is delivered by TASK-07",
                )
            else:
                self.add(
                    "workspace-storage-limit",
                    "resource",
                    self.workspace_storage_limit,
                    requires=workspace_required,
                )
            self.add(
                "container-startup-failure",
                "failure-recovery",
                self.startup_failure,
                attempts=3,
            )
            self.add(
                "container-identity-mismatch",
                "failure-recovery",
                self.identity_mismatch,
                attempts=3,
            )
            self.add(
                "parent-crash-cleanup",
                "failure-recovery",
                self.parent_crash_cleanup,
                attempts=3,
                requires=workspace_required,
            )
            self.add(
                "parent-crash-recovery",
                "failure-recovery",
                self.parent_crash_recovery,
                attempts=3,
                requires=workspace_required,
            )
            if self.daemon_fault_mode == "proxy":
                self.add(
                    "docker-daemon-unavailable",
                    "failure-recovery",
                    self.docker_daemon_unavailable,
                    attempts=3,
                    requires=workspace_required,
                )
            else:
                self.note(
                    "docker-daemon-unavailable",
                    "failure-recovery",
                    "unverified",
                    "daemon fault injection explicitly skipped",
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
            if self._fault_proxy is not None:
                try:
                    self._fault_proxy.close()
                except Exception as exc:
                    cleanup_errors.append(f"fault proxy: {type(exc).__name__}: {exc}")
            self._cleanup_fixture()
        try:
            source_after = _source_evidence(self.root, source_commit)
        except ValueError as exc:
            self.note(
                "source-evidence-stability",
                "harness",
                "failed",
                f"source evidence became unavailable after execution: {exc}",
            )
        else:
            if source_after != source_before:
                self.note(
                    "source-evidence-stability",
                    "harness",
                    "failed",
                    "source commit or source delta changed during acceptance execution",
                )
        return self._write_report(
            source_before,
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
    parser.add_argument("--workspace-limit-mb", type=int)
    parser.add_argument("--workspace-inode-limit", type=int, default=65_536)
    parser.add_argument("--docker-host")
    parser.add_argument("--daemon-fault-mode", choices=("proxy", "skip"), default="proxy")
    parser.add_argument("--source-commit", required=True)
    arguments = parser.parse_args()
    report = Matrix(
        arguments.root,
        arguments.output,
        arguments.workspace,
        workspace_limit_mb=arguments.workspace_limit_mb,
        workspace_inode_limit=arguments.workspace_inode_limit,
        docker_host=arguments.docker_host,
        daemon_fault_mode=arguments.daemon_fault_mode,
    ).run(arguments.source_commit)
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
