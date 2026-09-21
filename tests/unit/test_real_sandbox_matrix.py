from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import tests.acceptance.run_real_sandbox_matrix as matrix_module
from patchloop.sandbox import ManagedCommandIdentity, SandboxCleanupError
from tests.acceptance.run_real_sandbox_matrix import Matrix, _container_exists


def test_container_query_failure_is_not_reported_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(reference: str):
        raise SandboxCleanupError(f"daemon unavailable for {reference}")

    monkeypatch.setattr(matrix_module, "_inspect_docker_container", fail)

    with pytest.raises(SandboxCleanupError, match="daemon unavailable"):
        _container_exists("patchloop-command-1")


def test_cleanup_only_visits_containers_registered_by_this_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matrix = Matrix(tmp_path, tmp_path / "report.json")
    owned = ManagedCommandIdentity(
        id="owned-command",
        execution_id=matrix.execution_id,
        backend="docker",
        process_id=1,
        process_start_marker="marker",
        container_name="owned-container",
    )
    matrix._record_identity(owned)
    removed: list[str] = []
    monkeypatch.setattr(
        matrix_module,
        "_remove_verified_container",
        lambda identity: removed.append(identity.id) or True,
    )

    errors = matrix._cleanup_registered()

    assert errors == []
    assert removed == ["owned-command"]
    assert "unrelated-same-label" not in removed


def test_external_workspace_cleanup_preserves_mount_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    matrix = Matrix(root, tmp_path / "report.json", workspace)
    matrix.prepare()

    matrix._cleanup_fixture()

    assert workspace.is_dir()
    assert list(workspace.iterdir()) == []


def test_failed_positive_control_makes_dependent_probe_unverified(tmp_path: Path) -> None:
    matrix = Matrix(tmp_path, tmp_path / "report.json")
    matrix.note("workspace-read-write", "filesystem", "failed", "marker not writable")
    called = False

    def dependent_probe() -> dict[str, object]:
        nonlocal called
        called = True
        return {"passed": True}

    matrix.add(
        "symlink-toctou",
        "filesystem",
        dependent_probe,
        requires=("workspace-read-write",),
    )

    assert called is False
    assert matrix.cases[-1]["status"] == "unverified"


def test_internal_error_still_writes_schema_v2_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "report.json"
    matrix = Matrix(tmp_path, output)

    def fake_run(command: list[str], *, timeout: float = 30):
        del timeout
        stdout = ""
        if command[:3] == ["docker", "image", "inspect"]:
            stdout = "sha256:image\n"
        elif command[:2] == ["docker", "info"]:
            stdout = "[]\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(matrix_module, "_run", fake_run)
    monkeypatch.setattr(
        matrix,
        "add",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("probe boom")),
    )

    report = matrix.run("deadbeef")

    assert output.exists()
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema_version"] == persisted["schema_version"] == 2
    assert persisted["run_id"] == matrix.run_id
    assert persisted["status"] == "failed"
    assert any(case["id"] == "matrix-internal-error" for case in persisted["cases"])


def test_existing_report_is_never_overwritten(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    output.write_text("baseline", encoding="utf-8")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        Matrix(tmp_path, output).run("deadbeef")

    assert output.read_text(encoding="utf-8") == "baseline"
