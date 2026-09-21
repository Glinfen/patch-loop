"""Real Git adapter boundaries and lossless porcelain parsing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from patchloop.workspace.git import (
    GitAdapter,
    GitCommandError,
    GitError,
    GitParseError,
    GitTimeout,
    GitUnavailable,
    parse_status,
)


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Workspace Test")
    git(tmp_path, "config", "user.email", "workspace@example.test")
    (tmp_path / "tracked.txt").write_text("baseline\n")
    git(tmp_path, "add", "--", "tracked.txt")
    git(tmp_path, "commit", "-m", "baseline")
    return tmp_path


def test_discovery_and_dirty_status(repository: Path) -> None:
    adapter = GitAdapter()
    child = repository / "child"
    child.mkdir()
    assert adapter.discover(child) == adapter.discover(repository)
    assert not adapter.status(repository).dirty
    (repository / "tracked.txt").write_text("staged\n")
    git(repository, "add", "tracked.txt")
    (repository / "tracked.txt").write_text("unstaged\n")
    (repository / "new file.txt").write_bytes(b"\0binary")
    states = {item.path: item for item in adapter.status(repository).paths}
    assert states["tracked.txt"].index == "M"
    assert states["tracked.txt"].worktree == "M"
    assert states["new file.txt"].untracked


def test_rename_delete_and_literal_names(repository: Path) -> None:
    git(repository, "mv", "tracked.txt", "quoted name.txt")
    item = GitAdapter().status(repository).paths[0]
    assert item.original_path == "tracked.txt"
    assert item.path == "quoted name.txt"
    (repository / "quoted name.txt").unlink()
    assert GitAdapter().status(repository).paths[0].worktree == "D"


def test_worktree_gitfile_and_source_isolation(repository: Path, tmp_path: Path) -> None:
    adapter = GitAdapter()
    target = tmp_path / "isolated"
    info = adapter.create_worktree(repository, target, "HEAD")
    assert info.detached
    assert (target / ".git").is_file()
    assert adapter.discover(target).identity == adapter.discover(repository).identity
    assert adapter.discover(target).git_dir != adapter.discover(repository).git_dir
    (target / "tracked.txt").write_text("isolated change")
    assert (repository / "tracked.txt").read_text() == "baseline\n"
    with pytest.raises(GitCommandError):
        adapter.remove_worktree(repository, target)
    (target / "tracked.txt").write_text("baseline\n")
    adapter.remove_worktree(repository, target)
    assert not target.exists()


@pytest.mark.parametrize(
    "data", [b"garbage\0", b"? foo", b"1 M N...\0", b"2 R. N... 1 1 1 a b R100 x\0"]
)
def test_malformed_porcelain_fails_closed(data: bytes) -> None:
    with pytest.raises(GitParseError):
        parse_status(data)


def test_porcelain_special_paths_and_submodules() -> None:
    state = parse_status(
        b'? "literal"\tline\nname\x00! ignored\x001 .M S.MU 160000 160000 160000 abc def sub\x00'
    )
    assert state.paths[0].path == '"literal"\tline\nname'
    assert state.paths[1].ignored
    assert state.paths[2].submodule == "S.MU"


def test_failures_and_option_injection(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(GitUnavailable):
        GitAdapter(executable="patchloop-no-such-git").status(repository)
    with pytest.raises(GitError):
        GitAdapter().resolve_revision(repository, "--help")
    with pytest.raises(GitCommandError):
        GitAdapter().resolve_revision(repository, "HEAD; echo injected")

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(GitTimeout):
        GitAdapter().status(repository)


def test_nested_repository_is_explicit(repository: Path) -> None:
    nested = repository / "nested"
    nested.mkdir()
    git(nested, "init")
    git(nested, "config", "user.name", "Nested")
    git(nested, "config", "user.email", "nested@example.test")
    git(nested, "commit", "--allow-empty", "-m", "nested")
    assert GitAdapter().discover(nested).identity != GitAdapter().discover(repository).identity


def test_ambient_git_overrides_are_removed(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", str(repository / "nonexistent"))
    assert not GitAdapter().status(repository).dirty
