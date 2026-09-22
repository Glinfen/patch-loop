from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import tests.acceptance.run_real_sandbox_matrix as matrix_module
from patchloop.sandbox import ManagedCommandIdentity, SandboxCleanupError
from tests.acceptance.run_real_sandbox_matrix import (
    Matrix,
    _container_exists,
    _source_evidence,
    _SourceEvidence,
    _validate_report,
)


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
        matrix_module,
        "_source_evidence",
        lambda root, expected: _SourceEvidence("a" * 40, True, "b" * 64),
    )
    monkeypatch.setattr(
        matrix,
        "add",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("probe boom")),
    )

    report = matrix.run("a" * 40)

    assert output.exists()
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema_version"] == persisted["schema_version"] == 2
    assert persisted["run_id"] == matrix.run_id
    assert persisted["status"] == "failed"
    assert any(case["id"] == "matrix-internal-error" for case in persisted["cases"])
    inconsistent = {**persisted, "summary": {"passed": 999}}
    with pytest.raises(ValueError, match="summary is inconsistent"):
        _validate_report(inconsistent)


def test_existing_report_is_never_overwritten(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    output.write_text("baseline", encoding="utf-8")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        Matrix(tmp_path, output).run("deadbeef")

    assert output.read_text(encoding="utf-8") == "baseline"


def test_workspace_capacity_options_reach_the_real_sandbox(tmp_path: Path) -> None:
    matrix = Matrix(
        tmp_path,
        tmp_path / "report.json",
        workspace_limit_mb=64,
        workspace_inode_limit=4096,
        daemon_fault_mode="skip",
    )

    assert matrix.sandbox.config.workspace_limit_mb == 64
    assert matrix.sandbox.config.workspace_inode_limit == 4096


def test_daemon_fault_skip_is_explicitly_unverified(tmp_path: Path) -> None:
    matrix = Matrix(tmp_path, tmp_path / "report.json", daemon_fault_mode="skip")

    matrix.note(
        "docker-daemon-unavailable",
        "failure-recovery",
        "unverified",
        "daemon fault injection explicitly skipped",
    )

    case = matrix.cases[-1]
    assert case["status"] == "unverified"
    assert "skipped" in case["evidence"][0]["detail"]


def test_fault_proxy_uses_short_run_scoped_socket_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[Path] = []

    class FakeProxy:
        def __init__(self, upstream: Path, control_dir: Path) -> None:
            del upstream
            observed.append(control_dir)

        def start(self) -> str:
            return "unix:///tmp/patchloop-proxy.sock"

    class PosixOS:
        name = "posix"

    monkeypatch.setattr(matrix_module, "os", PosixOS)
    monkeypatch.setattr(matrix_module.tempfile, "gettempdir", lambda: "/tmp")
    monkeypatch.setattr(matrix_module, "DockerFaultProxy", FakeProxy)
    matrix = Matrix(tmp_path, tmp_path / "report.json", daemon_fault_mode="proxy")

    matrix._start_fault_proxy()

    socket_path = observed[0] / "docker-proxy.sock"
    assert observed[0].parent == Path("/tmp").resolve()
    assert matrix.run_id[:16] in observed[0].name
    assert len(str(socket_path).encode()) < 108


def test_source_evidence_rejects_arbitrary_commit_and_hashes_untracked_source(
    tmp_path: Path,
) -> None:
    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()

    git("init", "-q")
    git("config", "user.name", "PatchLoop Test")
    git("config", "user.email", "patchloop@example.invalid")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-q", "-m", "test:初始化证据仓库")
    commit = git("rev-parse", "HEAD")

    with pytest.raises(ValueError, match="does not match actual HEAD"):
        _source_evidence(tmp_path, "0" * 40)
    clean = _source_evidence(tmp_path, commit)
    assert clean.dirty is False

    source = tmp_path / "src" / "new.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    dirty = _source_evidence(tmp_path, commit)
    assert dirty.dirty is True
    assert dirty.diff_sha256 != clean.diff_sha256

    result = tmp_path / "benchmarks" / "results" / "new.json"
    result.parent.mkdir(parents=True)
    result.write_text("{}\n", encoding="utf-8")
    assert _source_evidence(tmp_path, commit) == dirty
