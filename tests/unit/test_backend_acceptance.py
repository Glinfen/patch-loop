from pathlib import Path

from patchloop.evaluation.backend_acceptance import (
    AcceptanceBackend,
    AcceptanceCapability,
    AcceptanceStatus,
    BackendAcceptanceReport,
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
