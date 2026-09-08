"""SRF-07 recovery classification, quality gates, and final acceptance summary."""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.domain import utc_now
from patchloop.evaluation.backend_acceptance import BackendAcceptanceReport
from patchloop.evaluation.fault_matrix import (
    FaultArea,
    FaultMatrix,
    FaultMatrixReport,
    RecoveryResult,
)
from patchloop.evaluation.ownership_contention import OwnershipContentionReport
from patchloop.evaluation.real_provider_trial import RealProviderTrialReport
from patchloop.evaluation.safety_audit import SafetyAuditReport


class GateStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class QualityGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    argv: list[str] = Field(min_length=1)
    status: GateStatus
    exit_code: int | None
    duration_ms: float = Field(ge=0.0)
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


class QualityGateReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    status: GateStatus
    platform: str
    python_version: str
    results: list[QualityGateResult] = Field(min_length=1)
    generated_at: str = Field(default_factory=lambda: utc_now().isoformat())

    @model_validator(mode="after")
    def aggregate_is_consistent(self) -> Self:
        expected = (
            GateStatus.PASSED
            if all(item.status is GateStatus.PASSED for item in self.results)
            else GateStatus.FAILED
        )
        if self.status is not expected:
            raise ValueError("quality-gate aggregate status is inconsistent")
        return self


class QualityGateRunner:
    def __init__(self, root: Path, *, timeout_seconds: float = 600.0) -> None:
        self.root = root.resolve(strict=True)
        self.timeout_seconds = timeout_seconds

    def run(self) -> QualityGateReport:
        commands = [
            ("ruff_check", [sys.executable, "-m", "ruff", "check", "src", "tests"]),
            (
                "ruff_format",
                [sys.executable, "-m", "ruff", "format", "--check", "src", "tests"],
            ),
            ("mypy", [sys.executable, "-m", "mypy", "src"]),
            ("pytest", [sys.executable, "-m", "pytest", "-q"]),
        ]
        temporary_root = self.root / ".patchloop" / "srf07-final-temp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["TMP"] = str(temporary_root)
        environment["TEMP"] = str(temporary_root)
        results = [self._run_gate(name, argv, environment) for name, argv in commands]
        return QualityGateReport(
            status=(
                GateStatus.PASSED
                if all(item.status is GateStatus.PASSED for item in results)
                else GateStatus.FAILED
            ),
            platform=platform.platform(),
            python_version=platform.python_version(),
            results=results,
        )

    def _run_gate(
        self, name: str, argv: list[str], environment: dict[str, str]
    ) -> QualityGateResult:
        started = perf_counter()
        try:
            completed = subprocess.run(
                argv,
                cwd=self.root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return QualityGateResult(
                name=name,
                argv=argv,
                status=GateStatus.FAILED,
                exit_code=None,
                duration_ms=(perf_counter() - started) * 1_000,
                error=f"{type(exc).__name__}: {exc}",
            )
        return QualityGateResult(
            name=name,
            argv=argv,
            status=(GateStatus.PASSED if completed.returncode == 0 else GateStatus.FAILED),
            exit_code=completed.returncode,
            duration_ms=(perf_counter() - started) * 1_000,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error=(
                None
                if completed.returncode == 0
                else f"command exited with code {completed.returncode}"
            ),
        )


class RecoveryOutcome(StrEnum):
    AUTOMATICALLY_RECOVERED = "automatically_recovered"
    SAFELY_STOPPED_FOR_OPERATOR = "safely_stopped_for_operator"
    RECOVERY_FAILED = "recovery_failed"


class RecoveryClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    outcome: RecoveryOutcome
    attempts: int = Field(ge=1)
    failed_attempts: int = Field(ge=0)
    unknown_remote_usage: bool


class ComponentStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNVERIFIED = "unverified"


class AcceptanceComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: ComponentStatus
    report: str
    detail: str


class Srf07AcceptanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    status: ComponentStatus
    recovery_classifications: list[RecoveryClassification]
    recovery_counts: dict[RecoveryOutcome, int]
    unknown_remote_usage_fault_cases: int = Field(ge=0)
    real_provider_unknown_usage_calls: int = Field(ge=0)
    components: list[AcceptanceComponent]
    generated_at: str = Field(default_factory=lambda: utc_now().isoformat())

    @model_validator(mode="after")
    def aggregate_is_consistent(self) -> Self:
        if set(self.recovery_counts) != set(RecoveryOutcome):
            raise ValueError("acceptance report must count every recovery outcome")
        actual = {
            outcome: sum(item.outcome is outcome for item in self.recovery_classifications)
            for outcome in RecoveryOutcome
        }
        if self.recovery_counts != actual:
            raise ValueError("recovery outcome counts are inconsistent")
        component_statuses = {component.status for component in self.components}
        expected = (
            ComponentStatus.FAILED
            if ComponentStatus.FAILED in component_statuses
            else ComponentStatus.UNVERIFIED
            if ComponentStatus.UNVERIFIED in component_statuses
            else ComponentStatus.PASSED
        )
        if self.status is not expected:
            raise ValueError("SRF-07 aggregate status is inconsistent")
        return self


def classify_recoveries(
    matrix: FaultMatrix, report: FaultMatrixReport
) -> list[RecoveryClassification]:
    cases = {case.id: case for case in matrix.cases}
    classifications: list[RecoveryClassification] = []
    for result in report.results:
        case = cases[result.case_id]
        failed_attempts = sum(not attempt.passed for attempt in result.attempts)
        if failed_attempts:
            outcome = RecoveryOutcome.RECOVERY_FAILED
        elif case.recovery_result is RecoveryResult.RECOVERY_REQUIRED:
            outcome = RecoveryOutcome.SAFELY_STOPPED_FOR_OPERATOR
        else:
            outcome = RecoveryOutcome.AUTOMATICALLY_RECOVERED
        classifications.append(
            RecoveryClassification(
                case_id=case.id,
                outcome=outcome,
                attempts=len(result.attempts),
                failed_attempts=failed_attempts,
                unknown_remote_usage=FaultArea.MODEL_RESPONSE in case.coverage,
            )
        )
    return classifications


def build_acceptance_report(
    *,
    matrix: FaultMatrix,
    fault_report: FaultMatrixReport,
    ownership_report: OwnershipContentionReport,
    backend_report: BackendAcceptanceReport,
    safety_report: SafetyAuditReport,
    real_provider_report: RealProviderTrialReport,
    quality_report: QualityGateReport,
    report_paths: dict[str, str],
) -> Srf07AcceptanceReport:
    classifications = classify_recoveries(matrix, fault_report)
    components = [
        AcceptanceComponent(
            name="fault_matrix",
            status=(
                ComponentStatus.PASSED
                if fault_report.failed_attempts == 0
                else ComponentStatus.FAILED
            ),
            report=report_paths["fault_matrix"],
            detail=(
                f"{fault_report.passed_cases}/{fault_report.total_cases} cases; "
                f"{fault_report.total_attempts} retained attempts"
            ),
        ),
        AcceptanceComponent(
            name="ownership_contention",
            status=(ComponentStatus.PASSED if ownership_report.passed else ComponentStatus.FAILED),
            report=report_paths["ownership_contention"],
            detail=(
                f"{ownership_report.contenders} processes x "
                f"{ownership_report.rounds_per_scope} rounds for three scopes"
            ),
        ),
        AcceptanceComponent(
            name="backend_acceptance",
            status=ComponentStatus(backend_report.status.value),
            report=report_paths["backend_acceptance"],
            detail="Windows passed; unavailable real backends remain unverified",
        ),
        AcceptanceComponent(
            name="safety_audit",
            status=(ComponentStatus.PASSED if safety_report.passed else ComponentStatus.FAILED),
            report=report_paths["safety_audit"],
            detail="all recovery safety and disclosure counters must remain zero",
        ),
        AcceptanceComponent(
            name="real_provider_trials",
            status=ComponentStatus(real_provider_report.status.value),
            report=report_paths["real_provider_trials"],
            detail=(
                f"{len(real_provider_report.results)} retained trials; "
                f"credential_configured={real_provider_report.credential_configured}"
            ),
        ),
        AcceptanceComponent(
            name="quality_gates",
            status=(
                ComponentStatus.PASSED
                if quality_report.status is GateStatus.PASSED
                else ComponentStatus.FAILED
            ),
            report=report_paths["quality_gates"],
            detail=(
                f"{sum(item.status is GateStatus.PASSED for item in quality_report.results)}/"
                f"{len(quality_report.results)} gates passed"
            ),
        ),
    ]
    statuses = {component.status for component in components}
    status = (
        ComponentStatus.FAILED
        if ComponentStatus.FAILED in statuses
        else ComponentStatus.UNVERIFIED
        if ComponentStatus.UNVERIFIED in statuses
        else ComponentStatus.PASSED
    )
    return Srf07AcceptanceReport(
        status=status,
        recovery_classifications=classifications,
        recovery_counts={
            outcome: sum(item.outcome is outcome for item in classifications)
            for outcome in RecoveryOutcome
        },
        unknown_remote_usage_fault_cases=sum(item.unknown_remote_usage for item in classifications),
        real_provider_unknown_usage_calls=sum(
            result.usage.unknown_remote_usage_calls for result in real_provider_report.results
        ),
        components=components,
    )


def write_json_report(report: BaseModel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _load(path: Path, model: type[BaseModel]) -> BaseModel:
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build SRF-07 final acceptance evidence")
    subparsers = parser.add_subparsers(dest="command", required=True)
    gates = subparsers.add_parser("quality-gates")
    gates.add_argument("--root", type=Path, default=Path.cwd())
    gates.add_argument("--output", type=Path, required=True)
    gates.add_argument("--timeout", type=float, default=600.0)
    summary = subparsers.add_parser("summary")
    summary.add_argument("--root", type=Path, default=Path.cwd())
    summary.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command == "quality-gates":
        gate_report = QualityGateRunner(arguments.root, timeout_seconds=arguments.timeout).run()
        write_json_report(gate_report, arguments.output)
        print(gate_report.model_dump_json(indent=2))
        return 0 if gate_report.status is GateStatus.PASSED else 1

    root = arguments.root.resolve(strict=True)
    paths = {
        "fault_matrix": "benchmarks/results/srf07_fault_matrix_step2.json",
        "ownership_contention": "benchmarks/results/srf07_ownership_contention_step3.json",
        "backend_acceptance": "benchmarks/results/srf07_backend_acceptance_step4.json",
        "safety_audit": "benchmarks/results/srf07_safety_audit_step5.json",
        "real_provider_trials": "benchmarks/results/srf07_real_provider_trials_step6.json",
        "quality_gates": "benchmarks/results/srf07_quality_gates_step8.json",
    }
    matrix = FaultMatrix.model_validate_json(
        (root / "tests/fixtures/session_fault_matrix.json").read_text(encoding="utf-8")
    )
    acceptance_report = build_acceptance_report(
        matrix=matrix,
        fault_report=_load(root / paths["fault_matrix"], FaultMatrixReport),  # type: ignore[arg-type]
        ownership_report=_load(root / paths["ownership_contention"], OwnershipContentionReport),  # type: ignore[arg-type]
        backend_report=_load(root / paths["backend_acceptance"], BackendAcceptanceReport),  # type: ignore[arg-type]
        safety_report=_load(root / paths["safety_audit"], SafetyAuditReport),  # type: ignore[arg-type]
        real_provider_report=_load(root / paths["real_provider_trials"], RealProviderTrialReport),  # type: ignore[arg-type]
        quality_report=_load(root / paths["quality_gates"], QualityGateReport),  # type: ignore[arg-type]
        report_paths=paths,
    )
    write_json_report(acceptance_report, arguments.output)
    print(acceptance_report.model_dump_json(indent=2))
    if acceptance_report.status is ComponentStatus.PASSED:
        return 0
    if acceptance_report.status is ComponentStatus.FAILED:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
