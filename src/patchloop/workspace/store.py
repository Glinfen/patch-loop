"""Workspace tables and a SQLiteStore mixin sharing the runtime database."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from patchloop.sqlite_support import connect, connect_write
from patchloop.workspace.models import (
    ChangeRecord,
    CommitPlan,
    VerificationRecord,
    WorkspaceBaseline,
    WorkspaceHandle,
)

WORKSPACE_MIGRATION = (
    """CREATE TABLE IF NOT EXISTS workspaces (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
        version INTEGER NOT NULL, payload_json TEXT NOT NULL, baseline_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS workspaces_session ON workspaces(session_id, status)",
    """CREATE UNIQUE INDEX IF NOT EXISTS workspaces_active_session ON workspaces(session_id)
       WHERE status != 'closed'""",
    """CREATE TABLE IF NOT EXISTS workspace_changes (
        workspace_id TEXT NOT NULL REFERENCES workspaces(id), path TEXT NOT NULL,
        payload_json TEXT NOT NULL, PRIMARY KEY(workspace_id, path)
    )""",
    """CREATE TABLE IF NOT EXISTS workspace_verifications (
        id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        created_at TEXT NOT NULL, payload_json TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS workspace_verification_time "
    "ON workspace_verifications(workspace_id, created_at)",
    """CREATE TABLE IF NOT EXISTS workspace_commit_plans (
        id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        payload_json TEXT NOT NULL
    )""",
)


@runtime_checkable
class WorkspaceStore(Protocol):
    def create_workspace(self, handle: WorkspaceHandle) -> WorkspaceHandle: ...
    def get_workspace(self, workspace_id: str) -> WorkspaceHandle: ...
    def list_workspaces(self, session_id: str | None = None) -> list[WorkspaceHandle]: ...
    def update_workspace(self, handle: WorkspaceHandle) -> WorkspaceHandle: ...
    def save_baseline(self, baseline: WorkspaceBaseline) -> None: ...
    def get_baseline(self, workspace_id: str) -> WorkspaceBaseline: ...
    def save_change(self, change: ChangeRecord) -> None: ...
    def list_changes(self, workspace_id: str) -> list[ChangeRecord]: ...
    def save_verification(self, verification: VerificationRecord) -> None: ...
    def list_verifications(self, workspace_id: str) -> list[VerificationRecord]: ...
    def save_commit_plan(self, plan: CommitPlan) -> None: ...
    def get_commit_plan(self, plan_id: str) -> CommitPlan: ...


class SQLiteWorkspaceMixin:
    path: Path

    def create_workspace(self, handle: WorkspaceHandle) -> WorkspaceHandle:
        with connect_write(self.path) as connection:
            connection.execute(
                "INSERT INTO workspaces(id, session_id, status, version, payload_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    handle.id,
                    handle.session_id,
                    handle.status,
                    handle.version,
                    handle.model_dump_json(),
                ),
            )
        return handle

    def get_workspace(self, workspace_id: str) -> WorkspaceHandle:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
        if row is None:
            raise KeyError(workspace_id)
        return WorkspaceHandle.model_validate_json(row[0])

    def list_workspaces(self, session_id: str | None = None) -> list[WorkspaceHandle]:
        with connect(self.path) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM workspaces WHERE (? IS NULL OR session_id = ?) "
                "ORDER BY id",
                (session_id, session_id),
            ).fetchall()
        return [WorkspaceHandle.model_validate_json(row[0]) for row in rows]

    def update_workspace(self, handle: WorkspaceHandle) -> WorkspaceHandle:
        updated = handle.model_copy(
            update={"version": handle.version + 1, "updated_at": datetime.now(UTC)}
        )
        with connect_write(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM workspaces WHERE id = ?", (handle.id,)
            ).fetchone()
            if row is None:
                raise KeyError(handle.id)
            existing = WorkspaceHandle.model_validate_json(row[0])
            if (existing.session_id, existing.mode, existing.repository) != (
                handle.session_id,
                handle.mode,
                handle.repository,
            ):
                raise ValueError("workspace identity binding is immutable")
            cursor = connection.execute(
                "UPDATE workspaces SET status = ?, version = ?, payload_json = ? "
                "WHERE id = ? AND version = ?",
                (
                    updated.status,
                    updated.version,
                    updated.model_dump_json(),
                    handle.id,
                    handle.version,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale workspace version")
        return updated

    def save_baseline(self, baseline: WorkspaceBaseline) -> None:
        with connect_write(self.path) as connection:
            existing = connection.execute(
                "SELECT baseline_json FROM workspaces WHERE id = ?", (baseline.workspace_id,)
            ).fetchone()
            if existing is None:
                raise KeyError(baseline.workspace_id)
            if existing[0] is not None:
                if WorkspaceBaseline.model_validate_json(existing[0]) != baseline:
                    raise ValueError("workspace baseline is immutable")
                return
            connection.execute(
                "UPDATE workspaces SET baseline_json = ? WHERE id = ?",
                (baseline.model_dump_json(), baseline.workspace_id),
            )

    def get_baseline(self, workspace_id: str) -> WorkspaceBaseline:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT baseline_json FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
        if row is None or row[0] is None:
            raise KeyError(workspace_id)
        return WorkspaceBaseline.model_validate_json(row[0])

    def save_change(self, change: ChangeRecord) -> None:
        with connect_write(self.path) as connection:
            connection.execute(
                "INSERT INTO workspace_changes VALUES (?, ?, ?) "
                "ON CONFLICT(workspace_id, path) DO UPDATE SET payload_json=excluded.payload_json",
                (change.workspace_id, change.path, change.model_dump_json()),
            )

    def list_changes(self, workspace_id: str) -> list[ChangeRecord]:
        with connect(self.path) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM workspace_changes WHERE workspace_id = ? ORDER BY path",
                (workspace_id,),
            ).fetchall()
        return [ChangeRecord.model_validate_json(row[0]) for row in rows]

    def save_verification(self, verification: VerificationRecord) -> None:
        with connect_write(self.path) as connection:
            connection.execute(
                "INSERT INTO workspace_verifications VALUES (?, ?, ?, ?)",
                (
                    verification.id,
                    verification.workspace_id,
                    verification.created_at.isoformat(),
                    verification.model_dump_json(),
                ),
            )

    def list_verifications(self, workspace_id: str) -> list[VerificationRecord]:
        with connect(self.path) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM workspace_verifications WHERE workspace_id = ? "
                "ORDER BY created_at DESC",
                (workspace_id,),
            ).fetchall()
        return [VerificationRecord.model_validate_json(row[0]) for row in rows]

    def save_commit_plan(self, plan: CommitPlan) -> None:
        with connect_write(self.path) as connection:
            connection.execute(
                "INSERT INTO workspace_commit_plans VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json",
                (plan.id, plan.workspace_id, plan.model_dump_json()),
            )

    def get_commit_plan(self, plan_id: str) -> CommitPlan:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM workspace_commit_plans WHERE id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            raise KeyError(plan_id)
        return CommitPlan.model_validate_json(row[0])
