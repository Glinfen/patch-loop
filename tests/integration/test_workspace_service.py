"""Persistent workspace workflows against real local Git repositories."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest

from patchloop.domain import Task
from patchloop.execution.approvals import ApprovalPending, ApprovalService
from patchloop.execution.ownership import ExecutionOwnershipManager
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import LeaseLost
from patchloop.workspace.models import OwnershipKind, VerificationInput
from patchloop.workspace.ownership import WorkspaceConflict, file_state
from patchloop.workspace.service import WorkspaceService


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


@pytest.fixture
def service(tmp_path: Path) -> Iterator[WorkspaceService]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Workspace Test")
    git(repo, "config", "user.email", "workspace@example.test")
    (repo / "agent.txt").write_bytes(b"baseline\n")
    (repo / "user.txt").write_bytes(b"user baseline\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    store = SQLiteStore(tmp_path / "state.db")
    task = store.prepare_task_execution(Task(goal="workspace maintenance", repository=str(repo)))
    manager = ExecutionOwnershipManager(store)
    ownership = manager.acquire(
        session_id=task.session_id or "", task_id=task.id, owner_id="test", repository=repo
    )
    try:
        yield WorkspaceService(store, manager=manager, ownership=ownership)
    finally:
        with suppress(LeaseLost):
            manager.release(ownership)


def open_direct(service: WorkspaceService) -> str:
    assert service.ownership is not None
    task = service.store.get_task(service.ownership.execution.task_id)
    return service.open(task.session_id or "", Path(task.repository)).id


def edit(service: WorkspaceService, workspace_id: str, path: str, content: bytes | None) -> None:
    ledger = service.ledger(workspace_id)
    target = ledger.handle.effective_root / path
    before = file_state(target)
    if content is None:
        target.unlink()
    else:
        target.write_bytes(content)
    ledger.record_effect("test-effect", target, before, file_state(target))


def approve_pending(service: WorkspaceService, pending: ApprovalPending) -> None:
    ApprovalService(service.store).decide_current(pending.approval.id, approved=True, source="test")


def test_dirty_baseline_restart_mixed_and_revert(service: WorkspaceService) -> None:
    assert service.ownership is not None
    repo = Path(service.store.get_task(service.ownership.execution.task_id).repository)
    (repo / "user.txt").write_bytes(b"user dirty\r\n")
    workspace_id = open_direct(service)
    edit(service, workspace_id, "agent.txt", b"agent update\n")
    restarted = WorkspaceService(SQLiteStore(service.store.path), ownership=service.ownership)
    records = {item.path: item for item in restarted.ledger(workspace_id).refresh()}
    assert records["user.txt"].ownership == OwnershipKind.USER
    assert records["agent.txt"].ownership == OwnershipKind.AGENT
    with pytest.raises(ApprovalPending) as pending:
        restarted.revert(workspace_id, ["agent.txt"])
    approve_pending(restarted, pending.value)
    restarted.revert(workspace_id, ["agent.txt"])
    assert (repo / "agent.txt").read_bytes() == b"baseline\n"
    assert (repo / "user.txt").read_bytes() == b"user dirty\r\n"
    edit(restarted, workspace_id, "agent.txt", b"agent update\n")
    (repo / "agent.txt").write_bytes(b"external\n")
    with pytest.raises(WorkspaceConflict):
        restarted.ledger(workspace_id).revert(["agent.txt"])
    assert (repo / "agent.txt").read_bytes() == b"external\n"


def test_verified_commit_preserves_user_index(service: WorkspaceService) -> None:
    assert service.ownership is not None
    repo = Path(service.store.get_task(service.ownership.execution.task_id).repository)
    (repo / "user.txt").write_bytes(b"user staged\n")
    git(repo, "add", "user.txt")
    (repo / "untracked.txt").write_bytes(b"user untracked\n")
    index = (repo / ".git" / "index").read_bytes()
    workspace_id = open_direct(service)
    edit(service, workspace_id, "agent.txt", b"accepted agent\n")
    service.accept(workspace_id, ["agent.txt"])
    with pytest.raises(ApprovalPending) as pending:
        service.verify(workspace_id, VerificationInput(command=[sys.executable, "--version"]))
    approve_pending(service, pending.value)
    record = service.verify(workspace_id, VerificationInput(command=[sys.executable, "--version"]))
    assert record.returncode == 0
    plan = service.prepare_commit(workspace_id, "feat:工作区测试")
    with pytest.raises(ApprovalPending) as pending:
        service.commit(workspace_id, plan.id)
    approve_pending(service, pending.value)
    result = service.commit(workspace_id, plan.id)
    assert result["commit"] == git(repo, "rev-parse", "HEAD").decode().strip()
    assert git(repo, "show", "HEAD:user.txt") == b"user baseline\n"
    assert git(repo, "show", "HEAD:agent.txt") == b"accepted agent\n"
    assert (repo / ".git" / "index").read_bytes() == index
    assert (repo / "untracked.txt").read_bytes() == b"user untracked\n"
    assert service.commit(workspace_id, plan.id) == result


def test_external_head_change_requires_recovery(service: WorkspaceService) -> None:
    workspace_id = open_direct(service)
    root = service.store.get_workspace(workspace_id).effective_root
    git(root, "commit", "--allow-empty", "-m", "external")
    with pytest.raises(WorkspaceConflict):
        service.diff(workspace_id)
    assert service.store.get_workspace(workspace_id).status == "recovery_required"


def test_create_delete_and_binary_revert(service: WorkspaceService) -> None:
    workspace_id = open_direct(service)
    root = service.store.get_workspace(workspace_id).effective_root
    edit(service, workspace_id, "new.bin", b"\x00\xff")
    edit(service, workspace_id, "agent.txt", None)
    service.ledger(workspace_id).revert(["new.bin", "agent.txt"])
    assert not (root / "new.bin").exists()
    assert (root / "agent.txt").read_bytes() == b"baseline\n"


def test_user_baseline_never_accepted_or_reverted(service: WorkspaceService) -> None:
    assert service.ownership is not None
    root = Path(service.store.get_task(service.ownership.execution.task_id).repository)
    (root / "user.txt").write_bytes(b"preexisting")
    workspace_id = open_direct(service)
    edit(service, workspace_id, "user.txt", b"agent touched user file")
    for operation in (service.ledger(workspace_id).accept, service.ledger(workspace_id).revert):
        with pytest.raises(WorkspaceConflict):
            operation(["user.txt"])
    assert (root / "user.txt").read_bytes() == b"agent touched user file"


def test_approval_bound_to_current_diff(service: WorkspaceService) -> None:
    workspace_id = open_direct(service)
    edit(service, workspace_id, "agent.txt", b"first")
    with pytest.raises(ApprovalPending) as pending:
        service.revert(workspace_id, ["agent.txt"])
    approve_pending(service, pending.value)
    edit(service, workspace_id, "agent.txt", b"second")
    # A fresh request may require a newly acquired execution after yielding.
    assert service.ownership is not None
    task_id = service.ownership.execution.task_id
    service.manager.release(service.ownership)
    task = service.store.get_task(task_id)
    service.ownership = service.manager.acquire(
        session_id=task.session_id or "",
        task_id=task.id,
        owner_id="new-owner",
        repository=task.repository,
    )
    with pytest.raises(ApprovalPending) as changed:
        service.revert(workspace_id, ["agent.txt"])
    assert changed.value.approval.id != pending.value.approval.id
    service.manager.release(service.ownership)


def test_lease_lost_fences_ledger(service: WorkspaceService) -> None:
    from patchloop.persistence_contracts import LeaseLost

    workspace_id = open_direct(service)
    ledger = service.ledger(workspace_id)
    assert service.ownership is not None
    service.manager.release(service.ownership)
    with pytest.raises(LeaseLost):
        ledger.refresh()


def test_schema6_migration_preserves_session_and_workspace_roundtrip(tmp_path: Path) -> None:
    from patchloop.session.service import SessionService
    from patchloop.sqlite_support import connect, connect_write, runtime_schema_version

    path = tmp_path / "state.db"
    store = SQLiteStore(path)
    session = SessionService(store).create(str(tmp_path))
    with connect_write(path) as connection:
        for table in (
            "workspace_commit_plans",
            "workspace_verifications",
            "workspace_changes",
            "workspaces",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute(
            "UPDATE patchloop_schema_migrations SET version=6 WHERE component='runtime'"
        )
    upgraded = SQLiteStore(path)
    assert upgraded.get_session(session.id) == session
    with connect(path) as connection:
        assert runtime_schema_version(connection) == 7


def test_policy_denial_prevents_git_or_file_mutation(service: WorkspaceService) -> None:
    from patchloop.execution.policy import PolicyAction, PolicyRule, PolicyRuleEffect, ResourceKind

    workspace_id = open_direct(service)
    edit(service, workspace_id, "agent.txt", b"agent")
    assert service.ownership is not None
    task = service.store.get_task(service.ownership.execution.task_id)
    service.store.put_policy_rule(
        PolicyRule(
            id="deny-workspace",
            source="session",
            session_id=task.session_id,
            workspace_ref=task.repository,
            action=PolicyAction.GIT,
            resource_kind=ResourceKind.WORKSPACE,
            pattern=workspace_id,
            effect=PolicyRuleEffect.DENY,
            policy_version="1",
        )
    )
    with pytest.raises(PermissionError):
        service.revert(workspace_id, ["agent.txt"])
    assert (Path(task.repository) / "agent.txt").read_bytes() == b"agent"
    assert not service.store.list_approvals(task.id)


def test_verification_stales_after_external_change(service: WorkspaceService) -> None:
    workspace_id = open_direct(service)
    edit(service, workspace_id, "agent.txt", b"accepted")
    service.accept(workspace_id, ["agent.txt"])
    request = VerificationInput(command=[sys.executable, "--version"])
    with pytest.raises(ApprovalPending) as pending:
        service.verify(workspace_id, request)
    approve_pending(service, pending.value)
    service.verify(workspace_id, request)
    root = service.store.get_workspace(workspace_id).effective_root
    (root / "extra").write_bytes(b"unknown change")
    with pytest.raises(WorkspaceConflict, match="verification"):
        service.prepare_commit(workspace_id, "feat:变更")
    (root / "extra").unlink()
    from datetime import UTC, datetime
    from uuid import uuid4

    successful = service.store.list_verifications(workspace_id)[0]
    service.store.save_verification(
        successful.model_copy(
            update={
                "id": uuid4().hex,
                "returncode": 1,
                "created_at": datetime.now(UTC),
            }
        )
    )
    with pytest.raises(WorkspaceConflict, match="verification"):
        service.prepare_commit(workspace_id, "feat:变更")


def test_interrupted_operation_requires_recovery(service: WorkspaceService) -> None:
    workspace_id = open_direct(service)
    handle = service.store.get_workspace(workspace_id)
    handle.active_effect_id = "interrupted-effect"
    service.store.update_workspace(handle)
    with pytest.raises(WorkspaceConflict, match="interrupted"):
        service.diff(workspace_id)
    assert service.store.get_workspace(workspace_id).status == "recovery_required"


def test_dirty_source_refuses_worktree_before_approval(service: WorkspaceService) -> None:
    from patchloop.workspace.models import WorkspaceMode

    assert service.ownership is not None
    task = service.store.get_task(service.ownership.execution.task_id)
    root = Path(task.repository)
    (root / "agent.txt").write_bytes(b"dirty source")
    with pytest.raises(WorkspaceConflict, match="clean source"):
        service.open(task.session_id or "", root, mode=WorkspaceMode.WORKTREE)
    assert not service.store.list_approvals(task.id)


def test_binary_tracked_file_restored_from_git_blob(service: WorkspaceService) -> None:
    assert service.ownership is not None
    root = Path(service.store.get_task(service.ownership.execution.task_id).repository)
    (root / "binary").write_bytes(b"\x00\xffbaseline")
    git(root, "add", "binary")
    git(root, "commit", "-m", "binary baseline")
    workspace_id = open_direct(service)
    edit(service, workspace_id, "binary", b"\x00\xffchanged")
    service.ledger(workspace_id).revert(["binary"])
    assert (root / "binary").read_bytes() == b"\x00\xffbaseline"


def test_dirty_worktree_cleanup_failure_preserves_directory(service: WorkspaceService) -> None:
    from patchloop.workspace.git import GitCommandError
    from patchloop.workspace.models import WorkspaceMode

    assert service.ownership is not None
    task = service.store.get_task(service.ownership.execution.task_id)
    root = Path(task.repository)
    with pytest.raises(ApprovalPending) as pending:
        service.open(task.session_id or "", root, mode=WorkspaceMode.WORKTREE)
    approve_pending(service, pending.value)
    handle = service.open(task.session_id or "", root, mode=WorkspaceMode.WORKTREE)
    task = service.store.get_task(task.id)
    service.store.update_task(
        task.model_copy(update={"repository": str(handle.effective_root)}),
        expected_version=task.version,
        lease_guard=service.ownership.lease_guard,
    )
    service.manager.release(service.ownership)
    service.ownership = service.manager.acquire(
        session_id=task.session_id or "",
        task_id=task.id,
        owner_id="worktree-owner",
        repository=handle.effective_root,
    )
    try:
        target = handle.effective_root / "untracked"
        target.write_bytes(b"must survive cleanup")
        with pytest.raises(ApprovalPending) as pending:
            service.close(handle.id)
        approve_pending(service, pending.value)
        with pytest.raises(GitCommandError):
            service.close(handle.id)
        assert target.read_bytes() == b"must survive cleanup"
        failed = service.store.get_workspace(handle.id)
        assert failed.status == "recovery_required"
        assert failed.cleanup_status == "failed"
    finally:
        service.manager.release(service.ownership)
