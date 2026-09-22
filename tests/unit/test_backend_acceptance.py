from pathlib import Path

from patchloop.evaluation.backend_acceptance import (
    AcceptanceBackend,
    AcceptanceCapability,
    AcceptanceStatus,
    BackendAcceptanceCase,
    BackendAcceptanceReport,
    BackendAcceptanceResult,
    BackendAcceptanceRunner,
    BackendProbe,
    load_backend_acceptance_matrix,
)

MATRIX_PATH = Path(__file__).parents[1] / "fixtures" / "session_backend_acceptance.json"
REPORT_PATH = (
    Path(__file__).parents[2] / "benchmarks" / "results" / ("srf07_backend_acceptance_step4.json")
)


def test_backend_acceptance_matrix_covers_each_capability() -> None:
    matrix = load_backend_acceptance_matrix(MATRIX_PATH)

    assert matrix.schema_version == 1
    for backend in AcceptanceBackend:
        cases = [case for case in matrix.cases if case.backend is backend]
        covered = {capability for case in cases for capability in case.capabilities}
        assert covered == set(AcceptanceCapability)
        for case in cases:
            path_text, separator, test_name = case.test_nodeid.partition("::")
            assert separator and test_name.startswith("test_")
            path = Path(path_text)
            assert path.is_file(), case.test_nodeid
            assert f"def {test_name}(" in path.read_text(encoding="utf-8")


def test_published_backend_report_does_not_treat_unavailable_docker_as_passed() -> None:
    matrix = load_backend_acceptance_matrix(MATRIX_PATH)
    report = BackendAcceptanceReport.model_validate_json(REPORT_PATH.read_text(encoding="utf-8"))

    assert report.matrix_schema_version == matrix.schema_version
    assert [result.case_id for result in report.results] == [case.id for case in matrix.cases]
    windows = [result for result in report.results if result.backend is AcceptanceBackend.WINDOWS]
    docker = [result for result in report.results if result.backend is AcceptanceBackend.DOCKER]
    assert windows and all(result.status is AcceptanceStatus.PASSED for result in windows)
    assert docker and all(result.status is AcceptanceStatus.UNVERIFIED for result in docker)
    assert report.status is AcceptanceStatus.UNVERIFIED
    docker_probe = next(
        probe for probe in report.probes if probe.backend is AcceptanceBackend.DOCKER
    )
    assert not docker_probe.available
    assert "unverified" in docker_probe.detail


def test_required_docker_can_pass_while_full_matrix_stays_unverified() -> None:
    report = BackendAcceptanceReport(
        matrix_schema_version=1,
        status=AcceptanceStatus.UNVERIFIED,
        required_backends=[AcceptanceBackend.DOCKER],
        target_status=AcceptanceStatus.PASSED,
        platform="linux",
        python_version="3.13",
        probes=[],
        results=[
            BackendAcceptanceResult(
                case_id="docker-real",
                backend=AcceptanceBackend.DOCKER,
                status=AcceptanceStatus.PASSED,
                duration_ms=1,
                detail="passed",
            ),
            BackendAcceptanceResult(
                case_id="windows-missing",
                backend=AcceptanceBackend.WINDOWS,
                status=AcceptanceStatus.UNVERIFIED,
                duration_ms=0,
                detail="requires Windows",
            ),
        ],
    )

    assert report.status is AcceptanceStatus.UNVERIFIED
    assert report.target_status is AcceptanceStatus.PASSED


def test_schema_v1_report_without_target_fields_remains_readable() -> None:
    payload = {
        "schema_version": 1,
        "matrix_schema_version": 1,
        "status": "unverified",
        "platform": "linux",
        "python_version": "3.13",
        "probes": [],
        "results": [
            {
                "case_id": "docker-missing",
                "backend": "docker",
                "status": "unverified",
                "duration_ms": 0,
                "detail": "missing",
            }
        ],
    }

    report = BackendAcceptanceReport.model_validate(payload)

    assert report.target_status is AcceptanceStatus.UNVERIFIED
    assert report.required_backends == list(AcceptanceBackend)


def test_zero_or_skipped_junit_tests_cannot_pass(
    tmp_path: Path,
) -> None:
    (tmp_path / "test_sample.py").write_text(
        "import pytest\n\ndef test_case():\n    pytest.skip('not exercised')\n",
        encoding="utf-8",
    )
    case = BackendAcceptanceCase(
        id="windows-skipped",
        backend=AcceptanceBackend.WINDOWS,
        capabilities=[AcceptanceCapability.PROCESS_TREE_CLEANUP],
        test_nodeid="test_sample.py::test_case",
    )

    result = BackendAcceptanceRunner(tmp_path)._run_case(
        case,
        BackendProbe(
            backend=AcceptanceBackend.WINDOWS,
            available=True,
            detail="test probe",
        ),
    )

    assert result.exit_code == 0
    assert result.status is AcceptanceStatus.FAILED
    assert result.junit is not None
    assert result.junit.tests == result.junit.skipped == 1


def test_available_docker_probe_without_image_identity_fails_closed(tmp_path: Path) -> None:
    case = BackendAcceptanceCase(
        id="docker-image-missing",
        backend=AcceptanceBackend.DOCKER,
        capabilities=[AcceptanceCapability.PROCESS_TREE_CLEANUP],
        test_nodeid="does-not-run.py::test_case",
    )

    result = BackendAcceptanceRunner(tmp_path)._run_case(
        case,
        BackendProbe(
            backend=AcceptanceBackend.DOCKER,
            available=True,
            detail="Docker available",
        ),
    )

    assert result.status is AcceptanceStatus.FAILED
    assert result.exit_code is None
    assert "image reference" in result.detail
