import subprocess
from pathlib import Path

import patchloop.evaluation.safety_audit as audit_module
from patchloop.evaluation.safety_audit import (
    SafetyAuditReport,
    SafetyAuditRunner,
    SafetyMetric,
    load_safety_audit_matrix,
)

MATRIX_PATH = Path(__file__).parents[1] / "fixtures" / "session_safety_audit.json"
REPORT_PATH = (
    Path(__file__).parents[2] / "benchmarks" / "results" / ("srf07_safety_audit_step5.json")
)


def test_safety_audit_matrix_covers_all_metrics_and_references_tests() -> None:
    matrix = load_safety_audit_matrix(MATRIX_PATH)

    covered = {metric for case in matrix.cases for metric in case.metrics}
    assert covered == set(SafetyMetric)
    for case in matrix.cases:
        path_text, separator, test_name = case.test_nodeid.partition("::")
        assert separator and test_name.startswith("test_")
        path = Path(path_text)
        assert path.is_file(), case.test_nodeid
        assert f"def {test_name}(" in path.read_text(encoding="utf-8")


def test_safety_audit_runs_all_checks_and_attributes_failures(
    monkeypatch,
) -> None:
    matrix = load_safety_audit_matrix(MATRIX_PATH)
    calls: list[str] = []

    def execute(command, **kwargs):
        del kwargs
        nodeid = command[-1]
        calls.append(nodeid)
        return subprocess.CompletedProcess(
            command,
            1 if len(calls) == 1 else 0,
            "failed" if len(calls) == 1 else "passed",
            "",
        )

    monkeypatch.setattr(audit_module.subprocess, "run", execute)

    report = SafetyAuditRunner(Path.cwd()).run(matrix)

    assert calls == [case.test_nodeid for case in matrix.cases]
    assert not report.passed
    first_metrics = set(matrix.cases[0].metrics)
    assert all(report.failed_checks_by_metric[metric] == 1 for metric in first_metrics)
    assert all(
        report.failed_checks_by_metric[metric] == 0 for metric in set(SafetyMetric) - first_metrics
    )


def test_published_step5_report_has_zero_safety_failures() -> None:
    matrix = load_safety_audit_matrix(MATRIX_PATH)
    report = SafetyAuditReport.model_validate_json(REPORT_PATH.read_text(encoding="utf-8"))

    assert report.matrix_schema_version == matrix.schema_version
    assert report.passed
    assert [result.case_id for result in report.results] == [case.id for case in matrix.cases]
    assert all(result.passed for result in report.results)
    assert report.failed_checks_by_metric == {metric: 0 for metric in SafetyMetric}
