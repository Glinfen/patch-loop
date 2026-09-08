from pathlib import Path

from patchloop.evaluation.backend_acceptance import BackendAcceptanceReport
from patchloop.evaluation.fault_matrix import FaultMatrix, FaultMatrixReport
from patchloop.evaluation.ownership_contention import OwnershipContentionReport
from patchloop.evaluation.real_provider_trial import RealProviderTrialReport
from patchloop.evaluation.safety_audit import SafetyAuditReport
from patchloop.evaluation.srf07_acceptance import (
    ComponentStatus,
    GateStatus,
    QualityGateReport,
    QualityGateResult,
    RecoveryOutcome,
    Srf07AcceptanceReport,
    build_acceptance_report,
    classify_recoveries,
)

ROOT = Path(__file__).parents[2]
RESULTS = ROOT / "benchmarks" / "results"


def _report(name: str, model):
    return model.model_validate_json((RESULTS / name).read_text(encoding="utf-8"))


def test_fault_results_distinguish_automatic_operator_and_failed_recovery() -> None:
    matrix = FaultMatrix.model_validate_json(
        (ROOT / "tests/fixtures/session_fault_matrix.json").read_text(encoding="utf-8")
    )
    report = _report("srf07_fault_matrix_step2.json", FaultMatrixReport)

    classified = classify_recoveries(matrix, report)

    assert len(classified) == len(matrix.cases)
    assert any(item.outcome is RecoveryOutcome.AUTOMATICALLY_RECOVERED for item in classified)
    assert any(item.outcome is RecoveryOutcome.SAFELY_STOPPED_FOR_OPERATOR for item in classified)
    assert all(item.outcome is not RecoveryOutcome.RECOVERY_FAILED for item in classified)
    remote_unknown = [item for item in classified if item.unknown_remote_usage]
    assert [item.case_id for item in remote_unknown] == ["model-response-unpersisted"]


def test_final_aggregate_preserves_external_unverified_components() -> None:
    matrix = FaultMatrix.model_validate_json(
        (ROOT / "tests/fixtures/session_fault_matrix.json").read_text(encoding="utf-8")
    )
    quality = QualityGateReport(
        status=GateStatus.PASSED,
        platform="test",
        python_version="3.12",
        results=[
            QualityGateResult(
                name="pytest",
                argv=["pytest", "-q"],
                status=GateStatus.PASSED,
                exit_code=0,
                duration_ms=1,
            )
        ],
    )
    paths = {
        "fault_matrix": "fault.json",
        "ownership_contention": "ownership.json",
        "backend_acceptance": "backend.json",
        "safety_audit": "safety.json",
        "real_provider_trials": "provider.json",
        "quality_gates": "quality.json",
    }

    report = build_acceptance_report(
        matrix=matrix,
        fault_report=_report("srf07_fault_matrix_step2.json", FaultMatrixReport),
        ownership_report=_report(
            "srf07_ownership_contention_step3.json", OwnershipContentionReport
        ),
        backend_report=_report("srf07_backend_acceptance_step4.json", BackendAcceptanceReport),
        safety_report=_report("srf07_safety_audit_step5.json", SafetyAuditReport),
        real_provider_report=_report(
            "srf07_real_provider_trials_step6.json", RealProviderTrialReport
        ),
        quality_report=quality,
        report_paths=paths,
    )

    assert report.status is ComponentStatus.UNVERIFIED
    indexed = {component.name: component.status for component in report.components}
    assert indexed["backend_acceptance"] is ComponentStatus.UNVERIFIED
    assert indexed["real_provider_trials"] is ComponentStatus.UNVERIFIED
    assert indexed["quality_gates"] is ComponentStatus.PASSED
    assert report.recovery_counts[RecoveryOutcome.RECOVERY_FAILED] == 0


def test_published_quality_and_acceptance_reports_keep_unverified_boundaries() -> None:
    quality = _report("srf07_quality_gates_step8.json", QualityGateReport)
    acceptance = _report("srf07_acceptance_summary.json", Srf07AcceptanceReport)

    assert quality.status is GateStatus.PASSED
    assert all(result.status is GateStatus.PASSED for result in quality.results)
    assert acceptance.status is ComponentStatus.UNVERIFIED
    indexed = {component.name: component.status for component in acceptance.components}
    assert indexed["backend_acceptance"] is ComponentStatus.UNVERIFIED
    assert indexed["real_provider_trials"] is ComponentStatus.UNVERIFIED
    assert indexed["quality_gates"] is ComponentStatus.PASSED
    assert acceptance.recovery_counts == {
        RecoveryOutcome.AUTOMATICALLY_RECOVERED: 7,
        RecoveryOutcome.SAFELY_STOPPED_FOR_OPERATOR: 4,
        RecoveryOutcome.RECOVERY_FAILED: 0,
    }
