"""SRF-07 recovery safety and cross-source consistency audit."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.domain import utc_now


class SafetyMetric(StrEnum):
    CONFIRMED_EFFECT_REEXECUTIONS = "confirmed_effect_reexecutions"
    UNKNOWN_AUTOMATIC_RETRIES = "unknown_automatic_retries"
    UNAUTHORIZED_ACTIONS = "unauthorized_actions"
    STALE_OWNER_SUCCESSFUL_COMMITS = "stale_owner_successful_commits"
    APPROVAL_DECISION_FAILURES = "approval_decision_failures"
    EVENT_INTEGRITY_FAILURES = "event_integrity_failures"
    CROSS_SOURCE_MISMATCHES = "cross_source_mismatches"
    CREDENTIAL_LEAKS = "credential_leaks"
    LEASE_TOKEN_LEAKS = "lease_token_leaks"


class SafetyAuditCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    metrics: list[SafetyMetric] = Field(min_length=1)
    test_nodeid: str


class SafetyAuditMatrix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    cases: list[SafetyAuditCase] = Field(min_length=1)

    @model_validator(mode="after")
    def cases_are_complete(self) -> Self:
        identifiers = [case.id for case in self.cases]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("safety audit case ids must be unique")
        covered = {metric for case in self.cases for metric in case.metrics}
        required: set[SafetyMetric] = {metric for metric in SafetyMetric}
        if covered != required:
            missing = sorted(metric.value for metric in required - covered)
            raise ValueError(f"safety audit metrics missing: {missing}")
        return self


class SafetyAuditCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    passed: bool
    exit_code: int | None = None
    duration_ms: float = Field(ge=0.0)
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


class SafetyAuditReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    matrix_schema_version: int
    passed: bool
    failed_checks_by_metric: dict[SafetyMetric, int]
    results: list[SafetyAuditCaseResult]
    generated_at: str = Field(default_factory=lambda: utc_now().isoformat())

    @model_validator(mode="after")
    def aggregate_is_consistent(self) -> Self:
        if set(self.failed_checks_by_metric) != set(SafetyMetric):
            raise ValueError("safety report must include every metric")
        expected = all(result.passed for result in self.results) and all(
            count == 0 for count in self.failed_checks_by_metric.values()
        )
        if self.passed != expected:
            raise ValueError("safety report pass aggregate is inconsistent")
        return self


class SafetyAuditRunner:
    def __init__(self, root: Path, *, timeout_seconds: float = 120.0) -> None:
        self.root = root.resolve(strict=True)
        self.timeout_seconds = timeout_seconds

    def run(self, matrix: SafetyAuditMatrix) -> SafetyAuditReport:
        results = [self._run_case(case) for case in matrix.cases]
        failed_by_metric = {metric: 0 for metric in SafetyMetric}
        for case, result in zip(matrix.cases, results, strict=True):
            if not result.passed:
                for metric in case.metrics:
                    failed_by_metric[metric] += 1
        return SafetyAuditReport(
            matrix_schema_version=matrix.schema_version,
            passed=all(result.passed for result in results),
            failed_checks_by_metric=failed_by_metric,
            results=results,
        )

    def _run_case(self, case: SafetyAuditCase) -> SafetyAuditCaseResult:
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
            return SafetyAuditCaseResult(
                case_id=case.id,
                passed=False,
                duration_ms=(perf_counter() - started) * 1_000,
                stdout=_subprocess_text(exc.stdout),
                stderr=_subprocess_text(exc.stderr),
                error=f"audit timed out after {self.timeout_seconds:g} seconds",
            )
        return SafetyAuditCaseResult(
            case_id=case.id,
            passed=completed.returncode == 0,
            exit_code=completed.returncode,
            duration_ms=(perf_counter() - started) * 1_000,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error=(
                None
                if completed.returncode == 0
                else f"pytest exited with code {completed.returncode}"
            ),
        )


def load_safety_audit_matrix(path: Path) -> SafetyAuditMatrix:
    return SafetyAuditMatrix.model_validate_json(path.read_text(encoding="utf-8"))


def write_safety_audit_report(report: SafetyAuditReport, path: Path) -> None:
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
    parser = argparse.ArgumentParser(description="Run the SRF-07 recovery safety audit")
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--timeout", type=float, default=120.0)
    arguments = parser.parse_args()
    matrix = load_safety_audit_matrix(arguments.matrix)
    report = SafetyAuditRunner(arguments.root, timeout_seconds=arguments.timeout).run(matrix)
    write_safety_audit_report(report, arguments.output)
    print(report.model_dump_json(indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
