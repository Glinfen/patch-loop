from pathlib import Path

from patchloop.evaluation.ownership_contention import (
    OwnershipContentionReport,
    OwnershipContentionRunner,
    OwnershipScope,
)

RESULT_PATH = (
    Path(__file__).parents[2] / "benchmarks" / "results" / ("srf07_ownership_contention_step3.json")
)


def test_contention_runner_keeps_winner_until_all_contenders_return(tmp_path: Path) -> None:
    report = OwnershipContentionRunner(tmp_path / "contention", contenders=4, rounds=3).run()

    assert report.passed
    assert {result.scope for result in report.scopes} == set(OwnershipScope)
    for result in report.scopes:
        assert result.reacquired_after_release
        assert len(result.rounds) == 3
        assert all(item.acquired_count == 1 for item in result.rounds)
        assert all(item.conflict_count == 3 for item in result.rounds)
        assert all(item.error_count == 0 for item in result.rounds)
        assert all(item.all_contenders_returned_before_release for item in result.rounds)
    assert report.different_workspaces.acquired_count == 4
    assert report.different_workspaces.conflict_count == 0


def test_published_step3_report_contains_full_8_by_100_matrix() -> None:
    report = OwnershipContentionReport.model_validate_json(RESULT_PATH.read_text(encoding="utf-8"))

    assert report.passed
    assert report.contenders == 8
    assert report.rounds_per_scope == 100
    assert {result.scope for result in report.scopes} == set(OwnershipScope)
    for result in report.scopes:
        assert result.passed
        assert result.reacquired_after_release
        assert len(result.rounds) == 100
        assert all(item.acquired_count == 1 for item in result.rounds)
        assert all(item.conflict_count == 7 for item in result.rounds)
        assert all(item.error_count == 0 for item in result.rounds)
        assert all(item.all_contenders_returned_before_release for item in result.rounds)
    assert report.different_workspaces.passed
    assert report.different_workspaces.acquired_count == 8
    assert report.different_workspaces.conflict_count == 0
    assert report.different_workspaces.error_count == 0
