"""Persistent workspace contracts, independent of runtime and storage adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceMode(StrEnum):
    DIRECT = "direct"
    WORKTREE = "worktree"


class WorkspaceStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    RECOVERY_REQUIRED = "recovery_required"


class OwnershipKind(StrEnum):
    USER = "user_preexisting"
    AGENT = "agent"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class WorkspaceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RevisionRef(WorkspaceModel):
    head: str | None
    branch: str | None
    detached: bool = False


class RepositoryInfo(WorkspaceModel):
    repository_root: Path
    git_dir: Path
    common_dir: Path
    identity: str
    revision: RevisionRef


class PathStatus(WorkspaceModel):
    path: str
    original_path: str | None = None
    index: str = "."
    worktree: str = "."
    submodule: str = "N..."
    untracked: bool = False
    ignored: bool = False
    conflicted: bool = False


class RepositoryState(WorkspaceModel):
    paths: list[PathStatus] = Field(default_factory=list)

    @property
    def dirty(self) -> bool:
        return any(not item.ignored for item in self.paths)


class WorktreeInfo(WorkspaceModel):
    path: Path
    head: str | None = None
    branch: str | None = None
    detached: bool = False
    locked: bool = False
    prunable: bool = False


class FileState(WorkspaceModel):
    digest: str | None = None
    content_base64: str | None = None
    kind: str = "missing"
    mode: int | None = None


class WorkspaceBaseline(WorkspaceModel):
    workspace_id: str
    revision: RevisionRef
    status: RepositoryState
    files: dict[str, FileState] = Field(default_factory=dict)
    digest: str
    index_digest: str | None = None


class WorkspaceHandle(WorkspaceModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    session_id: str
    repository: RepositoryInfo
    effective_root: Path
    mode: WorkspaceMode = WorkspaceMode.DIRECT
    base_revision: str | None = None
    baseline_digest: str = ""
    status: WorkspaceStatus = WorkspaceStatus.OPEN
    legacy_direct: bool = False
    managed_worktree: bool = False
    worktree_git_dir: Path | None = None
    owner_nonce: str = Field(default_factory=lambda: uuid4().hex)
    lease_owner: str | None = None
    lease_generation: int | None = None
    cleanup_status: str = "not_required"
    active_effect_id: str | None = None
    recovery_advice: str | None = None
    version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ChangeRecord(WorkspaceModel):
    workspace_id: str
    path: str
    baseline: FileState
    current_digest: str | None = None
    agent_expected_digest: str | None = None
    ownership: OwnershipKind = OwnershipKind.UNKNOWN
    effect_ids: list[str] = Field(default_factory=list)
    accepted_digest: str | None = None
    accepted: bool = False
    reason: str = ""


class VerificationInput(WorkspaceModel):
    command: list[str]


class VerificationRecord(WorkspaceModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    workspace_id: str
    effective_root: Path
    head: str
    digest: str
    command: list[str]
    returncode: int
    result_summary: str
    policy_version: str
    config_version: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CommitPlan(WorkspaceModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    workspace_id: str
    message: str
    head: str
    digest: str
    paths: list[str]
    verification_id: str
    policy_version: str
    config_version: str
    commit_revision: str | None = None
