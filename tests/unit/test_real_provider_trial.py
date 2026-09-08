from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from patchloop.evaluation.real_provider_trial import (
    RealProviderTrialManifest,
    RealProviderTrialReport,
    RealProviderTrialRunner,
    TrialStatus,
    load_real_provider_trial_manifest,
)

MANIFEST_PATH = Path(__file__).parents[1] / "fixtures" / "session_real_provider_trial.json"
REPORT_PATH = (
    Path(__file__).parents[2] / "benchmarks" / "results" / "srf07_real_provider_trials_step6.json"
)


def test_manifest_locks_real_repository_dependencies_and_three_trials() -> None:
    manifest = load_real_provider_trial_manifest(MANIFEST_PATH)

    assert manifest.trial_count == 3
    assert len(manifest.repository.revision) == 40
    assert manifest.repository.url.startswith("https://github.com/")
    assert all("==" in dependency for dependency in manifest.repository.dependencies)
    assert manifest.issue.goal
    assert manifest.issue.additional_constraint
    assertion = Path(manifest.independent_validation[0].argv[1])
    assert assertion.is_file()


def test_manifest_rejects_unlocked_dependency() -> None:
    payload = load_real_provider_trial_manifest(MANIFEST_PATH).model_dump(mode="json")
    payload["repository"]["dependencies"] = ["pytest>=8"]

    with pytest.raises(ValidationError, match="exact == versions"):
        RealProviderTrialManifest.model_validate(payload)


def test_missing_credentials_retains_three_unverified_results_without_workspaces(
    tmp_path: Path,
) -> None:
    manifest = load_real_provider_trial_manifest(MANIFEST_PATH)
    work_root = tmp_path / "real-trials"

    report = RealProviderTrialRunner(tmp_path, work_root, environment={}).run(manifest)

    assert report.status is TrialStatus.UNVERIFIED
    assert not report.credential_configured
    assert len(report.results) == 3
    assert all(result.status is TrialStatus.UNVERIFIED for result in report.results)
    assert all(result.stage == "preflight" for result in report.results)
    assert all(not result.commands for result in report.results)
    assert not work_root.exists()


def test_command_evidence_redacts_configured_credentials(tmp_path: Path) -> None:
    runner = RealProviderTrialRunner(tmp_path, tmp_path / "runs", environment={})
    runner._secrets = ["srf07-secret"]

    evidence = runner._command(
        [str(Path(__file__)), "srf07-secret"],
        tmp_path,
    )

    serialized = evidence.model_dump_json()
    assert "srf07-secret" not in serialized
    assert "[REDACTED]" in serialized


def test_published_report_keeps_real_provider_trials_unverified_without_credentials() -> None:
    manifest = load_real_provider_trial_manifest(MANIFEST_PATH)
    report = RealProviderTrialReport.model_validate_json(REPORT_PATH.read_text(encoding="utf-8"))

    assert report.manifest_schema_version == manifest.schema_version
    assert report.status is TrialStatus.UNVERIFIED
    assert report.provider == manifest.provider.name
    assert report.model == manifest.provider.model
    assert report.revision == manifest.repository.revision
    assert not report.credential_configured
    assert [result.trial for result in report.results] == [1, 2, 3]
    assert all(result.status is TrialStatus.UNVERIFIED for result in report.results)
