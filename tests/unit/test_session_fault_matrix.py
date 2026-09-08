from pathlib import Path

from patchloop.evaluation.fault_matrix import (
    FaultAttemptResult,
    FaultMatrixReport,
    FaultMatrixRunner,
    write_fault_matrix_report,
)
from tests.support.session_fault_matrix import FaultArea, FaultBackend, load_fault_matrix

MATRIX_PATH = Path(__file__).parents[1] / "fixtures" / "session_fault_matrix.json"
RESULT_PATH = (
    Path(__file__).parents[2] / "benchmarks" / "results" / ("srf07_fault_matrix_step2.json")
)


def test_srf07_fault_matrix_is_complete_and_references_tests() -> None:
    matrix = load_fault_matrix(MATRIX_PATH)
    covered = {area for case in matrix.cases for area in case.coverage}

    assert matrix.schema_version == 1
    assert matrix.repetitions == 3
    assert covered == set(FaultArea)
    assert {case.backend for case in matrix.cases} == set(FaultBackend)
    for case in matrix.cases:
        test_path_text, separator, test_name = case.test_nodeid.partition("::")
        assert separator and test_name.startswith("test_")
        test_path = Path(test_path_text)
        assert test_path.is_file(), case.test_nodeid
        function_name = test_name.partition("[")[0]
        assert f"def {function_name}(" in test_path.read_text(encoding="utf-8"), case.test_nodeid


def test_process_crash_cases_use_parent_controlled_barriers() -> None:
    matrix = load_fault_matrix(MATRIX_PATH)
    crash_points = {
        "tool_dispatched",
        "before_external_action",
        "after_external_action",
        "result_submitted",
        "before_checkpoint_commit",
    }

    indexed = {case.fault_point: case for case in matrix.cases}
    assert crash_points <= indexed.keys()
    assert all(indexed[point].parent_controlled for point in crash_points)


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def execute(self, case, attempt):
        self.calls.append((case.id, attempt))
        if case.id == "turn-event-rollback" and attempt == 1:
            return FaultAttemptResult(
                case_id=case.id,
                attempt=attempt,
                passed=False,
                exit_code=1,
                duration_ms=1.0,
                stdout="first failure",
                error="injected failure",
            )
        if case.id == "message-consumption-before-effect-claim" and attempt == 2:
            raise RuntimeError("injected executor crash")
        return FaultAttemptResult(
            case_id=case.id,
            attempt=attempt,
            passed=True,
            exit_code=0,
            duration_ms=1.0,
        )


def test_fault_matrix_runner_keeps_every_attempt_after_failures(tmp_path: Path) -> None:
    matrix = load_fault_matrix(MATRIX_PATH)
    executor = _RecordingExecutor()

    report = FaultMatrixRunner(executor).run(matrix)

    assert len(executor.calls) == len(matrix.cases) * matrix.repetitions
    assert report.total_attempts == len(executor.calls)
    assert report.failed_attempts == 2
    assert report.passed_cases == len(matrix.cases) - 2
    assert all(len(result.attempts) == matrix.repetitions for result in report.results)
    failures = [
        attempt for result in report.results for attempt in result.attempts if not attempt.passed
    ]
    assert [failure.attempt for failure in failures] == [1, 2]
    assert failures[0].stdout == "first failure"
    assert failures[1].error == "RuntimeError: injected executor crash"

    output = tmp_path / "reports" / "fault-matrix.json"
    write_fault_matrix_report(report, output)
    restored = type(report).model_validate_json(output.read_text(encoding="utf-8"))
    assert restored == report


def test_published_step2_report_retains_all_repetitions() -> None:
    matrix = load_fault_matrix(MATRIX_PATH)
    report = FaultMatrixReport.model_validate_json(RESULT_PATH.read_text(encoding="utf-8"))

    assert report.matrix_schema_version == matrix.schema_version
    assert report.repetitions == matrix.repetitions
    assert report.total_cases == len(matrix.cases)
    assert report.total_attempts == len(matrix.cases) * matrix.repetitions
    assert [result.case_id for result in report.results] == [case.id for case in matrix.cases]
    assert all(
        [attempt.attempt for attempt in result.attempts] == list(range(1, matrix.repetitions + 1))
        for result in report.results
    )
