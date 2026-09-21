"""Argument-vector Git adapter. No shell, network operations or ambient Git overrides."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

from patchloop.security import SecretRedactor
from patchloop.workspace.models import (
    PathStatus,
    RepositoryInfo,
    RepositoryState,
    RevisionRef,
    WorktreeInfo,
)


class GitError(RuntimeError):
    code = "git_error"


class GitUnavailable(GitError):
    code = "git_unavailable"


class GitTimeout(GitError):
    code = "git_timeout"


class GitParseError(GitError):
    code = "git_parse_error"


class GitCommandError(GitError):
    code = "git_command_failed"

    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = SecretRedactor().redact_text(stderr)[:2048]
        super().__init__(self.stderr or f"Git exited with status {returncode}")


def canonical_path(path: Path) -> Path:
    return Path(os.path.normcase(str(path.resolve(strict=True))))


def parse_status(data: bytes) -> RepositoryState:
    """Parse porcelain v2 -z; paths are literal bytes, never C-quoted in this mode."""
    if data and not data.endswith(b"\0"):
        raise GitParseError("unterminated porcelain record")
    records = iter(data.split(b"\0")[:-1])
    paths: list[PathStatus] = []
    for record in records:
        if record.startswith(b"# "):
            continue
        prefix = record[:1]
        if prefix in {b"?", b"!"}:
            if not record.startswith(prefix + b" ") or len(record) < 3:
                raise GitParseError("invalid untracked/ignored record")
            paths.append(
                PathStatus(
                    path=os.fsdecode(record[2:]), untracked=prefix == b"?", ignored=prefix == b"!"
                )
            )
            continue
        size = {b"1": 8, b"2": 9, b"u": 10}.get(prefix)
        if size is None:
            raise GitParseError("unknown porcelain record")
        parts = record.split(b" ", size)
        if len(parts) != size + 1 or len(parts[1]) != 2 or len(parts[2]) != 4:
            raise GitParseError("invalid porcelain fields")
        if not re.fullmatch(rb"[.MADRCUT?!]{2}", parts[1]):
            raise GitParseError("invalid porcelain status")
        if not re.fullmatch(rb"N\.\.\.|S[.C][.M][.U]", parts[2]):
            raise GitParseError("invalid submodule status")
        original = None
        if prefix == b"2":
            original_bytes = next(records, None)
            if not original_bytes:
                raise GitParseError("rename missing original path")
            original = os.fsdecode(original_bytes)
        if not parts[-1]:
            raise GitParseError("empty porcelain path")
        paths.append(
            PathStatus(
                path=os.fsdecode(parts[-1]),
                original_path=original,
                index=chr(parts[1][0]),
                worktree=chr(parts[1][1]),
                submodule=parts[2].decode("ascii"),
                conflicted=prefix == b"u",
            )
        )
    return RepositoryState(paths=paths)


class GitAdapter:
    def __init__(self, *, executable: str = "git", timeout: float = 30) -> None:
        self.executable = executable
        self.timeout = timeout

    def _run(
        self,
        repository: Path,
        args: list[str],
        *,
        input_data: bytes | None = None,
        index_file: Path | None = None,
    ) -> bytes:
        env = {
            key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
        }
        env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_LITERAL_PATHSPECS="1")
        if index_file is not None:
            env["GIT_INDEX_FILE"] = str(index_file.resolve())
        try:
            result = subprocess.run(
                [
                    self.executable,
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.untrackedCache=false",
                    *args,
                ],
                cwd=repository,
                env=env,
                input=input_data,
                capture_output=True,
                timeout=self.timeout,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise GitUnavailable("Git executable or repository directory is unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitTimeout("Git command timed out") from exc
        if result.returncode:
            raise GitCommandError(result.returncode, result.stderr.decode("utf-8", "replace"))
        return result.stdout

    def revision(self, repository: Path) -> RevisionRef:
        branch_data = self._run(repository, ["rev-parse", "--symbolic-full-name", "HEAD"])
        branch = branch_data.decode().strip()
        head = self.resolve_revision(repository, "HEAD")
        return RevisionRef(
            head=head, branch=None if branch == "HEAD" else branch, detached=branch == "HEAD"
        )

    def resolve_revision(self, repository: Path, revision: str) -> str:
        if not revision or revision.startswith("-") or "\0" in revision:
            raise GitError("invalid revision")
        value = (
            self._run(
                repository, ["rev-parse", "--verify", "--end-of-options", revision + "^{commit}"]
            )
            .decode()
            .strip()
        )
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
            raise GitParseError("invalid revision object ID")
        return value

    def discover(self, path: Path) -> RepositoryInfo:
        path = canonical_path(path)
        root = canonical_path(
            Path(
                os.fsdecode(
                    self._run(
                        path, ["rev-parse", "--path-format=absolute", "--show-toplevel"]
                    ).rstrip(b"\r\n")
                )
            )
        )
        git_dir = canonical_path(
            Path(os.fsdecode(self._run(root, ["rev-parse", "--absolute-git-dir"]).rstrip(b"\r\n")))
        )
        common = canonical_path(
            Path(
                os.fsdecode(
                    self._run(
                        root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
                    ).rstrip(b"\r\n")
                )
            )
        )
        return RepositoryInfo(
            repository_root=root,
            git_dir=git_dir,
            common_dir=common,
            identity=hashlib.sha256(os.fsencode(common)).hexdigest(),
            revision=self.revision(root),
        )

    def status(self, repository: Path) -> RepositoryState:
        return parse_status(
            self._run(
                repository,
                [
                    "status",
                    "--porcelain=v2",
                    "-z",
                    "--untracked-files=all",
                    "--ignore-submodules=none",
                ],
            )
        )

    def tracked_paths(self, repository: Path) -> list[str]:
        return [
            os.fsdecode(item)
            for item in self._run(repository, ["ls-files", "-z"]).split(b"\0")
            if item
        ]

    def blob(self, repository: Path, revision: str, relative: str) -> bytes:
        head = self.resolve_revision(repository, revision)
        return self._run(repository, ["cat-file", "blob", head + ":" + relative])

    def diff(self, repository: Path, relative: str, *, staged: bool = False) -> str:
        args = ["diff", "--no-ext-diff", "--no-textconv"]
        if staged:
            args.append("--cached")
        else:
            args.append("HEAD")
        return self._run(repository, [*args, "--", relative]).decode("utf-8", "replace")

    def worktrees(self, repository: Path) -> list[WorktreeInfo]:
        data = self._run(repository, ["worktree", "list", "--porcelain", "-z"])
        result: list[WorktreeInfo] = []
        for block in data.split(b"\0\0"):
            if not block:
                continue
            fields = block.split(b"\0")
            if not fields[0].startswith(b"worktree "):
                raise GitParseError("invalid worktree record")
            item = WorktreeInfo(path=Path(os.fsdecode(fields[0][9:])))
            for field in fields[1:]:
                key, _, value = field.partition(b" ")
                if key == b"HEAD":
                    item.head = value.decode("ascii")
                elif key == b"branch":
                    item.branch = os.fsdecode(value)
                elif key in {b"detached", b"locked", b"prunable"}:
                    setattr(item, key.decode(), True)
            result.append(item)
        return result

    def create_worktree(
        self,
        repository: Path,
        target: Path,
        revision: str,
        branch: str | None = None,
    ) -> WorktreeInfo:
        revision = self.resolve_revision(repository, revision)
        try:
            filters = self._run(
                repository,
                ["config", "--name-only", "--get-regexp", r"^filter\..*\.(smudge|process)$"],
            )
        except GitCommandError as exc:
            if exc.returncode != 1:
                raise
            filters = b""
        overrides: list[str] = []
        for key_bytes in filters.splitlines():
            key = key_bytes.decode("utf-8")
            overrides.extend(["-c", key + "=", "-c", key.rsplit(".", 1)[0] + ".required=false"])
        args = [*overrides, "worktree", "add"]
        if branch is None:
            args.append("--detach")
        else:
            if not branch.startswith("patchloop/"):
                raise GitError("managed branches must use patchloop/ prefix")
            self._run(repository, ["check-ref-format", "--branch", branch])
            args.extend(["-b", branch])
        self._run(repository, [*args, "--", str(target.resolve()), revision])
        return next(
            item
            for item in self.worktrees(repository)
            if canonical_path(item.path) == canonical_path(target)
        )

    def remove_worktree(self, repository: Path, target: Path) -> None:
        # Never force: dirty, locked and untracked worktrees must survive cleanup.
        self._run(repository, ["worktree", "remove", "--", str(target.resolve())])

    def build_commit(
        self,
        repository: Path,
        head: str,
        message: str,
        paths: dict[str, tuple[bytes, int] | None],
    ) -> str:
        """Build an unreachable commit without filters, hooks or touching the user's index."""
        with tempfile.TemporaryDirectory(prefix="patchloop-index-") as directory:
            index = Path(directory) / "index"
            self._run(repository, ["read-tree", head], index_file=index)
            for relative, content in sorted(paths.items()):
                if content is None:
                    self._run(
                        repository,
                        ["update-index", "--force-remove", "--", relative],
                        index_file=index,
                    )
                else:
                    data, mode = content
                    blob = (
                        self._run(repository, ["hash-object", "-w", "--stdin"], input_data=data)
                        .decode()
                        .strip()
                    )
                    self._run(
                        repository,
                        ["update-index", "--add", "--cacheinfo", f"{mode:o}", blob, relative],
                        index_file=index,
                    )
            tree = self._run(repository, ["write-tree"], index_file=index).decode().strip()
            return (
                self._run(
                    repository,
                    ["-c", "commit.gpgSign=false", "commit-tree", tree, "-p", head],
                    input_data=message.encode(),
                )
                .decode()
                .strip()
            )

    def advance_head(self, repository: Path, revision: str, expected_head: str) -> None:
        self._run(repository, ["update-ref", "HEAD", revision, expected_head])

    def synchronize_owned_index(self, repository: Path, revision: str, paths: list[str]) -> None:
        """Update only approved paths in an isolated worktree's own index."""
        self._run(repository, ["reset", "--quiet", revision, "--", *paths])
