"""Repeatable SRF-07 fault-matrix execution and machine-readable reports."""

from __future__ import annotations

import argparse
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.domain import utc_now


class FaultBackend(StrEnum):
    SQLITE = "sqlite"
    FILESYSTEM = "filesystem"
    PROVIDER = "provider"
    MEMORY = "memory"
    TRACE_JSONL = "trace_jsonl"


class FaultArea(StrEnum):
    MESSAGE_SUBMISSION = "message_submission"
    MESSAGE_CONSUMPTION = "message_consumption"
    MODEL_RESPONSE = "model_response"
    TOOL_BATCH = "tool_batch"
    EXECUTION_INTENT = "execution_intent"
    EXTERNAL_ACTION = "external_action"
    RESULT_COMMIT = "result_commit"
    CHECKPOINT_COMMIT = "checkpoint_commit"
    MEMORY_COMMIT = "memory_commit"
    TRACE_EXPORT = "trace_export"


class RecoveryResult(StrEnum):
    RESUMED = "resumed"
    REPLAYED = "replayed"
    RECOVERY_REQUIRED = "recovery_required"
    REPAIRED = "repaired"


class ExpectedState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_outcome: str
    runtime_condition: str
    effect_status: str | None = None


class EventIntegrity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_events: list[str] = Field(default_factory=list)
    forbidden_events: list[str] = Field(default_factory=list)
    trace_matches_journal: bool

    @model_validator(mode="after")
    def event_sets_do_not_overlap(self) -> Self:
        overlap = set(self.required_events) & set(self.forbidden_events)
        if overlap:
            raise ValueError(f"events cannot be both required and forbidden: {sorted(overlap)}")
        return self


class FaultMatrixCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    source_task: str = Field(pattern=r"^SRF-0[0-7]$")
    fault_point: str
    coverage: list[FaultArea] = Field(min_length=1)
    backend: FaultBackend
    test_nodeid: str
    parent_controlled: bool
    expected_state: ExpectedState
    independent_action_count: int = Field(ge=0)
    recovery_result: RecoveryResult
    event_integrity: EventIntegrity


class FaultMatrix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    repetitions: int = Field(ge=3)
    cases: list[FaultMatrixCase] = Field(min_length=1)

    @model_validator(mode="after")
    def case_ids_are_unique(self) -> Self:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("fault matrix case ids must be unique")
        covered = {area for case in self.cases for area in case.coverage}
        required: set[FaultArea] = {area for area in FaultArea}
        if covered != required:
            missing = sorted(area.value for area in required - covered)
            raise ValueError(f"fault matrix does not cover required areas: {missing}")
        return self


class FaultAttemptResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    attempt: int = Field(ge=1)
    passed: bool
    exit_code: int | None = None
    duration_ms: float = Field(ge=0.0)
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


class FaultCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    passed: bool
    attempts: list[FaultAttemptResult] = Field(min_length=3)


class FaultMatrixReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    matrix_schema_version: int
    repetitions: int
    total_cases: int
    passed_cases: int
    total_attempts: int
    failed_attempts: int
    results: list[FaultCaseResult]
    generated_at: str = Field(default_factory=lambda: utc_now().isoformat())

    @model_validator(mode="after")
    def aggregate_matches_attempts(self) -> Self:
        if self.total_cases != len(self.results):
            raise ValueError("fault report total_cases does not match results")
        if len({result.case_id for result in self.results}) != len(self.results):
            raise ValueError("fault report case ids must be unique")
        if any(len(result.attempts) != self.repetitions for result in self.results):
            raise ValueError("fault report must retain every configured repetition")
        attempts = [attempt for result in self.results for attempt in result.attempts]
        if self.total_attempts != len(attempts):
            raise ValueError("fault report total_attempts does not match attempts")
        if self.failed_attempts != sum(not attempt.passed for attempt in attempts):
            raise ValueError("fault report failed_attempts does not match attempts")
        if self.passed_cases != sum(result.passed for result in self.results):
            raise ValueError("fault report passed_cases does not match results")
        return self


class FaultCaseExecutor(Protocol):
    def execute(self, case: FaultMatrixCase, attempt: int) -> FaultAttemptResult: ...


class PytestFaultCaseExecutor:
    """Execute one matrix case in an isolated pytest subprocess."""

    def __init__(self, root: Path, *, timeout_seconds: float = 120.0) -> None:
        self.root = root.resolve(strict=True)
        self.timeout_seconds = timeout_seconds

    def execute(self, case: FaultMatrixCase, attempt: int) -> FaultAttemptResult:
        started = perf_counter()
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", case.test_nodeid],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            return FaultAttemptResult(
                case_id=case.id,
                attempt=attempt,
                passed=False,
                duration_ms=(perf_counter() - started) * 1_000,
                stdout=_subprocess_text(exc.stdout),
                stderr=_subprocess_text(exc.stderr),
                error=f"pytest timed out after {self.timeout_seconds:g} seconds",
            )
        return FaultAttemptResult(
            case_id=case.id,
            attempt=attempt,
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


class FaultMatrixRunner:
    """Run every configured repetition without stopping after failures."""

    def __init__(self, executor: FaultCaseExecutor) -> None:
        self.executor = executor

    def run(self, matrix: FaultMatrix) -> FaultMatrixReport:
        results: list[FaultCaseResult] = []
        for case in matrix.cases:
            attempts: list[FaultAttemptResult] = []
            for attempt in range(1, matrix.repetitions + 1):
                try:
                    result = self.executor.execute(case, attempt)
                except Exception as exc:
                    result = FaultAttemptResult(
                        case_id=case.id,
                        attempt=attempt,
                        passed=False,
                        duration_ms=0.0,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                if result.case_id != case.id or result.attempt != attempt:
                    raise ValueError(
                        "fault executor returned a result for the wrong case or attempt"
                    )
                attempts.append(result)
            results.append(
                FaultCaseResult(
                    case_id=case.id,
                    passed=all(attempt.passed for attempt in attempts),
                    attempts=attempts,
                )
            )
        all_attempts = [attempt for result in results for attempt in result.attempts]
        return FaultMatrixReport(
            matrix_schema_version=matrix.schema_version,
            repetitions=matrix.repetitions,
            total_cases=len(results),
            passed_cases=sum(result.passed for result in results),
            total_attempts=len(all_attempts),
            failed_attempts=sum(not attempt.passed for attempt in all_attempts),
            results=results,
        )


def load_fault_matrix(path: Path) -> FaultMatrix:
    return FaultMatrix.model_validate_json(path.read_text(encoding="utf-8"))


def write_fault_matrix_report(report: FaultMatrixReport, path: Path) -> None:
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
    parser = argparse.ArgumentParser(description="Run the SRF-07 fault matrix")
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--timeout", type=float, default=120.0)
    arguments = parser.parse_args()
    matrix = load_fault_matrix(arguments.matrix)
    report = FaultMatrixRunner(
        PytestFaultCaseExecutor(arguments.root, timeout_seconds=arguments.timeout)
    ).run(matrix)
    write_fault_matrix_report(report, arguments.output)
    print(report.model_dump_json(indent=2))
    return 0 if report.failed_attempts == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
