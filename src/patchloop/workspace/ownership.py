"""Byte-preserving baselines and conservative path-level attribution."""

from __future__ import annotations

import base64
import difflib
import hashlib
import os
import stat
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from uuid import uuid4

from patchloop.execution.policy import ResourceKind, ResourceSelector, digest
from patchloop.workspace.git import GitAdapter
from patchloop.workspace.models import (
    ChangeRecord,
    FileState,
    OwnershipKind,
    WorkspaceBaseline,
    WorkspaceHandle,
    WorkspaceMode,
)
from patchloop.workspace.store import WorkspaceStore


class WorkspaceConflict(RuntimeError):
    code = "workspace_conflict"


def safe_path(root: Path, relative: str) -> Path:
    relative = relative.replace("\\", "/")
    ResourceSelector(kind=ResourceKind.PATH, value=relative)
    parts = PurePosixPath(relative).parts
    if not parts or any(part in {".", ".."} for part in parts):
        raise WorkspaceConflict("invalid workspace selector")
    reserved = {
        "con",
        "nul",
        "prn",
        "aux",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
    if any(
        part != part.rstrip(" .") or part.split(".")[0].casefold() in reserved for part in parts
    ):
        raise WorkspaceConflict("ambiguous Windows selector")
    candidate = root
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink() or candidate.is_junction():
            raise WorkspaceConflict("symlink or junction selector is unsafe")
        if candidate.is_dir() and (candidate / ".git").exists():
            raise WorkspaceConflict("nested repository requires its own workspace")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise WorkspaceConflict("selector escapes workspace")
    return candidate


def file_state(path: Path) -> FileState:
    if path.is_symlink() or path.is_junction():
        return FileState(kind="unsafe", digest="unsafe")
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            return FileState(kind="unknown", digest="unknown")
        data = path.read_bytes()
    except FileNotFoundError:
        return FileState()
    except OSError:
        return FileState(kind="unknown", digest="unknown")
    try:
        data.decode("utf-8")
        binary = b"\0" in data
    except UnicodeDecodeError:
        binary = True
    mode = stat.S_IMODE(metadata.st_mode)
    return FileState(
        digest=hashlib.sha256(data + b"\0mode:" + str(mode).encode()).hexdigest(),
        content_base64=base64.b64encode(data).decode("ascii"),
        kind="binary" if binary else "text",
        mode=mode,
    )


def capture_baseline(handle: WorkspaceHandle, git: GitAdapter) -> WorkspaceBaseline:
    root = handle.effective_root
    revision = git.revision(root)
    before = git.status(root)
    paths = set(git.tracked_paths(root)) | {item.path for item in before.paths}
    files: dict[str, FileState] = {}
    for relative in sorted(paths):
        if relative.casefold().split("/")[0] == ".patchloop":
            continue
        try:
            files[relative] = file_state(safe_path(root, relative))
        except ValueError:
            # Protected files affect freshness, but their contents must not enter the database.
            candidate = root / relative
            chain = [candidate, *candidate.parents]
            within_root = candidate.resolve().is_relative_to(root.resolve())
            aliased = any(
                path.is_symlink() or path.is_junction()
                for path in chain
                if path.is_relative_to(root)
            )
            protected = file_state(candidate) if within_root and not aliased else FileState()
            files[relative] = FileState(kind="unknown", digest=protected.digest)
        except WorkspaceConflict:
            files[relative] = FileState(kind="unsafe", digest="unsafe")
    after = git.status(root)
    if before != after:
        raise WorkspaceConflict("repository changed while capturing baseline")
    if revision != git.revision(root):
        raise WorkspaceConflict("HEAD changed while capturing baseline")
    for relative, captured in files.items():
        if (
            captured.kind not in {"unknown", "unsafe"}
            and file_state(safe_path(root, relative)).digest != captured.digest
        ):
            raise WorkspaceConflict("file changed while capturing baseline")
    index = git.discover(root).git_dir / "index"
    return WorkspaceBaseline(
        workspace_id=handle.id,
        revision=revision,
        status=before,
        files=files,
        digest=digest({name: state.digest for name, state in files.items()}),
        index_digest=hashlib.sha256(index.read_bytes()).hexdigest() if index.exists() else None,
    )


class ChangeOwnershipLedger:
    def __init__(
        self,
        handle: WorkspaceHandle,
        store: WorkspaceStore,
        git: GitAdapter,
        assert_owned: Callable[[], None],
    ) -> None:
        self.handle = handle
        self.store = store
        self.git = git
        self.assert_owned = assert_owned
        self.baseline = store.get_baseline(handle.id)

    def record_effect(
        self,
        effect_id: str,
        path: Path,
        before: FileState,
        after: FileState,
    ) -> None:
        self.assert_owned()
        relative = path.relative_to(self.handle.effective_root).as_posix()
        safe_path(self.handle.effective_root, relative)
        records = {item.path: item for item in self.store.list_changes(self.handle.id)}
        original = self.baseline.files.get(relative, FileState())
        previous = records.get(relative)
        user_paths = {item.path for item in self.baseline.status.paths}
        expected = original.digest if previous is None else previous.agent_expected_digest
        mixed = (
            relative in user_paths
            or before.digest != expected
            or (previous is not None and previous.ownership == OwnershipKind.MIXED)
        )
        record = ChangeRecord(
            workspace_id=self.handle.id,
            path=relative,
            baseline=original,
            current_digest=after.digest,
            agent_expected_digest=after.digest,
            ownership=OwnershipKind.MIXED if mixed else OwnershipKind.AGENT,
            effect_ids=[*(previous.effect_ids if previous else []), effect_id],
            reason="agent touched user/external content" if mixed else "successful agent effect",
        )
        self.store.save_change(record)

    def refresh(self) -> list[ChangeRecord]:
        self.assert_owned()
        records = {item.path: item for item in self.store.list_changes(self.handle.id)}
        status = self.git.status(self.handle.effective_root)
        paths = set(records) | {item.path for item in status.paths}
        user_paths = {item.path for item in self.baseline.status.paths}
        result: list[ChangeRecord] = []
        for relative in sorted(paths):
            if relative.casefold().split("/")[0] == ".patchloop":
                continue
            try:
                current = file_state(safe_path(self.handle.effective_root, relative))
            except (ValueError, WorkspaceConflict):
                current = FileState(kind="unsafe", digest="unsafe")
            original = self.baseline.files.get(relative, FileState())
            record = records.get(relative)
            if record is None:
                owner = OwnershipKind.UNKNOWN
                if relative in user_paths:
                    owner = OwnershipKind.USER
                elif self.handle.mode == WorkspaceMode.WORKTREE:
                    owner = OwnershipKind.AGENT
                record = ChangeRecord(
                    workspace_id=self.handle.id,
                    path=relative,
                    baseline=original,
                    ownership=owner,
                    current_digest=current.digest,
                    agent_expected_digest=current.digest if owner == OwnershipKind.AGENT else None,
                    reason="baseline user change"
                    if owner == OwnershipKind.USER
                    else "isolated worktree change"
                    if owner == OwnershipKind.AGENT
                    else "no successful agent effect recorded",
                )
            if record.ownership == OwnershipKind.AGENT:
                if current.digest != record.agent_expected_digest:
                    record.ownership = OwnershipKind.MIXED
                    record.reason = "content changed after last agent effect"
            elif record.ownership == OwnershipKind.USER and current.digest != original.digest:
                record.ownership = OwnershipKind.MIXED
                record.reason = "user baseline changed outside recorded effects"
            if current.kind in {"unsafe", "unknown"}:
                record.ownership = OwnershipKind.UNKNOWN
                record.reason = "unsafe or unreadable path"
            if record.current_digest != current.digest:
                record.accepted = False
                record.accepted_digest = None
            record.current_digest = current.digest
            self.store.save_change(record)
            if current.digest != original.digest or relative in user_paths:
                result.append(record)
        return result

    def diff(self, scope: str = "default") -> dict[str, object]:
        scopes: dict[str, set[OwnershipKind]] = {
            "all": set(OwnershipKind),
            "default": {OwnershipKind.AGENT, OwnershipKind.MIXED},
            "agent": {OwnershipKind.AGENT},
            "mixed": {OwnershipKind.MIXED},
            "user": {OwnershipKind.USER},
        }
        if scope not in scopes:
            raise ValueError("unknown diff scope")
        records = self.refresh()
        sections: list[dict[str, object]] = []
        for record in records:
            if record.ownership not in scopes[scope]:
                continue
            text = ""
            git_diff = ""
            staged_diff = ""
            try:
                current = file_state(safe_path(self.handle.effective_root, record.path))
                git_diff = self.git.diff(self.handle.effective_root, record.path)
                staged_diff = self.git.diff(self.handle.effective_root, record.path, staged=True)
                if current.kind in {"text", "missing"} and record.baseline.kind in {
                    "text",
                    "missing",
                }:
                    before = base64.b64decode(record.baseline.content_base64 or "").decode("utf-8")
                    if record.ownership == OwnershipKind.USER and any(
                        item.path == record.path and item.untracked
                        for item in self.baseline.status.paths
                    ):
                        before = ""
                    after = base64.b64decode(current.content_base64 or "").decode("utf-8")
                    text = "".join(
                        difflib.unified_diff(
                            before.splitlines(keepends=True),
                            after.splitlines(keepends=True),
                            fromfile="a/" + record.path,
                            tofile="b/" + record.path,
                        )
                    )
            except (ValueError, WorkspaceConflict):
                pass
            item = record.model_dump(mode="json", exclude={"baseline"})
            item.update(baseline_digest=record.baseline.digest, diff=text)
            item.update(git_diff=git_diff, staged_diff=staged_diff)
            sections.append(item)
        return {
            "workspace_id": self.handle.id,
            "scope": scope,
            "paths": sections,
            "user_baseline_paths": [item.path for item in self.baseline.status.paths],
        }

    def accept(self, paths: Sequence[str]) -> None:
        records = self._selected(paths)
        for record in records:
            record.accepted = True
            record.accepted_digest = record.current_digest
            self.store.save_change(record)

    def _selected(self, paths: Sequence[str]) -> list[ChangeRecord]:
        current = {item.path: item for item in self.refresh()}
        result: list[ChangeRecord] = []
        for path in dict.fromkeys(paths):
            safe_path(self.handle.effective_root, path)
            record = current.get(path)
            if record is None or record.ownership != OwnershipKind.AGENT:
                raise WorkspaceConflict(f"path is not agent-only: {path}")
            result.append(record)
        if not result:
            raise WorkspaceConflict("no paths selected")
        return result

    def revert(self, paths: Sequence[str]) -> dict[str, object]:
        records = self._selected(paths)
        # Validate every selector before the first mutation.
        for record in records:
            path = safe_path(self.handle.effective_root, record.path)
            current = file_state(path)
            if current.digest != record.agent_expected_digest:
                raise WorkspaceConflict("path changed before revert")
            if record.baseline.kind not in {"text", "binary", "missing"}:
                raise WorkspaceConflict("unsupported baseline restoration")
            if record.baseline.kind == "binary":
                original = self.git.blob(
                    self.handle.effective_root, self.baseline.revision.head or "", record.path
                )
                if original != base64.b64decode(record.baseline.content_base64 or ""):
                    raise WorkspaceConflict("binary baseline differs from Git blob")
        for record in records:
            self.assert_owned()
            path = safe_path(self.handle.effective_root, record.path)
            if file_state(path).digest != record.agent_expected_digest:
                raise WorkspaceConflict("path changed during revert")
            if record.baseline.kind == "missing":
                path.unlink(missing_ok=True)
            else:
                temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
                try:
                    content = (
                        self.git.blob(
                            self.handle.effective_root,
                            self.baseline.revision.head or "",
                            record.path,
                        )
                        if record.baseline.kind == "binary"
                        else base64.b64decode(record.baseline.content_base64 or "")
                    )
                    temporary.write_bytes(content)
                    if record.baseline.mode is not None:
                        os.chmod(temporary, record.baseline.mode)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            record.agent_expected_digest = record.baseline.digest
            record.current_digest = record.baseline.digest
            record.accepted = False
            record.accepted_digest = None
            self.store.save_change(record)
        return {"reverted": [item.path for item in records], "conflicts": []}
