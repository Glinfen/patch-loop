"""SRF-07 target-platform and real-backend acceptance runner."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.domain import utc_now


class AcceptanceBackend(StrEnum):
    WINDOWS = "windows"
    DOCKER = "docker"


class AcceptanceCapability(StrEnum):
    PROCESS_TREE_CLEANUP = "process_tree_cleanup"
    OLD_WORKER_RECOVERY = "old_worker_recovery"
    LEASE_TAKEOVER = "lease_takeover"
    USER_MODIFIED_FILE = "user_modified_file"


class AcceptanceStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNVERIFIED = "unverified"


class BackendAcceptanceCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    backend: AcceptanceBackend
    capabilities: list[AcceptanceCapability] = Field(min_length=1)
    test_nodeid: str


class BackendAcceptanceMatrix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    docker_image: str = "patchloop-sandbox:py313"
    cases: list[BackendAcceptanceCase] = Field(min_length=1)

    @model_validator(mode="after")
    def cases_are_complete(self) -> Self:
        identifiers = [case.id for case in self.cases]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("backend acceptance case ids must be unique")
        required: set[AcceptanceCapability] = {item for item in AcceptanceCapability}
        for backend in AcceptanceBackend:
            covered = {
                capability
                for case in self.cases
                if case.backend is backend
                for capability in case.capabilities
            }
            if covered != required:
                missing = sorted(item.value for item in required - covered)
                raise ValueError(f"{backend.value} acceptance capabilities missing: {missing}")
        return self


class BackendProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: AcceptanceBackend
    available: bool
    detail: str


class BackendAcceptanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    backend: AcceptanceBackend
    status: AcceptanceStatus
    duration_ms: float = Field(ge=0.0)
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    detail: str


class BackendAcceptanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    matrix_schema_version: int
    status: AcceptanceStatus
    platform: str
    python_version: str
    probes: list[BackendProbe]
    results: list[BackendAcceptanceResult]
    generated_at: str = Field(default_factory=lambda: utc_now().isoformat())

    @model_validator(mode="after")
    def aggregate_is_consistent(self) -> Self:
        statuses = {result.status for result in self.results}
        expected = (
            AcceptanceStatus.FAILED
            if AcceptanceStatus.FAILED in statuses
            else (
                AcceptanceStatus.UNVERIFIED
                if AcceptanceStatus.UNVERIFIED in statuses
                else AcceptanceStatus.PASSED
            )
        )
        if self.status is not expected:
            raise ValueError("backend acceptance aggregate status is inconsistent")
        return self


class BackendAcceptanceRunner:
    def __init__(self, root: Path, *, timeout_seconds: float = 120.0) -> None:
        self.root = root.resolve(strict=True)
        self.timeout_seconds = timeout_seconds

    def run(self, matrix: BackendAcceptanceMatrix) -> BackendAcceptanceReport:
        probes = {
            AcceptanceBackend.WINDOWS: self._probe_windows(),
            AcceptanceBackend.DOCKER: self._probe_docker(matrix.docker_image),
        }
        results = [self._run_case(case, probes[case.backend]) for case in matrix.cases]
        statuses = {result.status for result in results}
        status = (
            AcceptanceStatus.FAILED
            if AcceptanceStatus.FAILED in statuses
            else (
                AcceptanceStatus.UNVERIFIED
                if AcceptanceStatus.UNVERIFIED in statuses
                else AcceptanceStatus.PASSED
            )
        )
        return BackendAcceptanceReport(
            matrix_schema_version=matrix.schema_version,
            status=status,
            platform=platform.platform(),
            python_version=platform.python_version(),
            probes=list(probes.values()),
            results=results,
        )

    def _run_case(
        self, case: BackendAcceptanceCase, probe: BackendProbe
    ) -> BackendAcceptanceResult:
        if not probe.available:
            return BackendAcceptanceResult(
                case_id=case.id,
                backend=case.backend,
                status=AcceptanceStatus.UNVERIFIED,
                duration_ms=0.0,
                detail=probe.detail,
            )
        started = perf_counter()
        environment = os.environ.copy()
        source_path = str(self.root / "src")
        environment["PYTHONPATH"] = os.pathsep.join(
            item for item in [source_path, environment.get("PYTHONPATH", "")] if item
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", case.test_nodeid],
                cwd=self.root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            return BackendAcceptanceResult(
                case_id=case.id,
                backend=case.backend,
                status=AcceptanceStatus.FAILED,
                duration_ms=(perf_counter() - started) * 1_000,
                stdout=_subprocess_text(exc.stdout),
                stderr=_subprocess_text(exc.stderr),
                detail=f"acceptance test timed out after {self.timeout_seconds:g} seconds",
            )
        return BackendAcceptanceResult(
            case_id=case.id,
            backend=case.backend,
            status=(
                AcceptanceStatus.PASSED if completed.returncode == 0 else AcceptanceStatus.FAILED
            ),
            duration_ms=(perf_counter() - started) * 1_000,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            detail=(
                "real backend acceptance passed"
                if completed.returncode == 0
                else f"pytest exited with code {completed.returncode}"
            ),
        )

    @staticmethod
    def _probe_windows() -> BackendProbe:
        available = os.name == "nt"
        return BackendProbe(
            backend=AcceptanceBackend.WINDOWS,
            available=available,
            detail=(
                "running on the target Windows host"
                if available
                else f"requires Windows; current os.name is {os.name}"
            ),
        )

    @staticmethod
    def _probe_docker(image: str) -> BackendProbe:
        executable = shutil.which("docker")
        if executable is None:
            return BackendProbe(
                backend=AcceptanceBackend.DOCKER,
                available=False,
                detail="Docker CLI is not installed or not on PATH; real backend unverified",
            )
        try:
            daemon = subprocess.run(
                [executable, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return BackendProbe(
                backend=AcceptanceBackend.DOCKER,
                available=False,
                detail=f"Docker daemon probe failed: {type(exc).__name__}: {exc}",
            )
        if daemon.returncode != 0:
            detail = (daemon.stderr or daemon.stdout).strip()
            return BackendProbe(
                backend=AcceptanceBackend.DOCKER,
                available=False,
                detail=f"Docker daemon is unavailable: {detail[:300]}",
            )
        image_probe = subprocess.run(
            [executable, "image", "inspect", image],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if image_probe.returncode != 0:
            return BackendProbe(
                backend=AcceptanceBackend.DOCKER,
                available=False,
                detail=f"required Docker image is unavailable: {image}",
            )
        return BackendProbe(
            backend=AcceptanceBackend.DOCKER,
            available=True,
            detail=f"Docker daemon and image available: {image}",
        )


def load_backend_acceptance_matrix(path: Path) -> BackendAcceptanceMatrix:
    return BackendAcceptanceMatrix.model_validate_json(path.read_text(encoding="utf-8"))


def write_backend_acceptance_report(report: BackendAcceptanceReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _subprocess_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SRF-07 backend acceptance")
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--timeout", type=float, default=120.0)
    arguments = parser.parse_args()
    matrix = load_backend_acceptance_matrix(arguments.matrix)
    report = BackendAcceptanceRunner(arguments.root, timeout_seconds=arguments.timeout).run(matrix)
    write_backend_acceptance_report(report, arguments.output)
    print(report.model_dump_json(indent=2))
    if report.status is AcceptanceStatus.PASSED:
        return 0
    if report.status is AcceptanceStatus.FAILED:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
