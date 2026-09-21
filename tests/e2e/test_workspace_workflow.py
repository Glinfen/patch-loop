"""Isolated worktree workflow via public CLI and persistent approvals."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from patchloop.cli import app
from patchloop.execution.approvals import ApprovalService
from patchloop.persistence import SQLiteStore
from patchloop.session.service import SessionService


def test_worktree_source_isolation_and_cleanup(tmp_path: Path) -> None:
    (tmp_path / "tracked").write_bytes(b"source\n")
    for args in (
        ["init"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.test"],
        ["add", "tracked"],
        ["commit", "-m", "baseline"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    source_index = (tmp_path / ".git" / "index").read_bytes()
    runner = CliRunner()
    created = runner.invoke(
        app, ["session", "--repo", str(tmp_path), "create", "--workspace-mode", "worktree"]
    )
    assert created.exit_code == 0, created.output
    store = SQLiteStore(tmp_path / ".patchloop" / "patchloop.db")
    session = store.list_sessions()[0]
    args = ["workspace", "open", session.id, "--repo", str(tmp_path), "--mode", "worktree"]
    pending = runner.invoke(app, args)
    assert pending.exit_code == 3, pending.output
    ApprovalService(store).decide_current(
        json.loads(pending.output)["approval_id"], approved=True, source="test"
    )
    opened = runner.invoke(app, args)
    assert opened.exit_code == 0, opened.output
    data = json.loads(opened.output)["data"]
    target = Path(data["effective_root"])
    (target / "tracked").write_bytes(b"agent\n")
    review = runner.invoke(app, ["workspace", "diff", data["id"], "--repo", str(tmp_path)])
    assert review.exit_code == 0, review.output
    assert json.loads(review.output)["data"]["paths"][0]["ownership"] == "agent"
    assert (tmp_path / "tracked").read_bytes() == b"source\n"
    assert (tmp_path / ".git" / "index").read_bytes() == source_index
    accepted = runner.invoke(
        app, ["workspace", "accept", data["id"], "--path", "tracked", "--repo", str(tmp_path)]
    )
    assert accepted.exit_code == 0, accepted.output
    verify_args = [
        "workspace",
        "verify",
        data["id"],
        "--repo",
        str(tmp_path),
        "--arg=python",
        "--arg=--version",
    ]
    pending = runner.invoke(app, verify_args)
    assert pending.exit_code == 3, pending.output
    ApprovalService(store).decide_current(
        json.loads(pending.output)["approval_id"], approved=True, source="test"
    )
    verified = runner.invoke(app, verify_args)
    assert verified.exit_code == 0, verified.output
    prepared = runner.invoke(
        app,
        ["workspace", "commit", data["id"], "--repo", str(tmp_path), "--message", "feat:隔离提交"],
    )
    assert prepared.exit_code == 0, prepared.output
    plan_id = json.loads(prepared.output)["data"]["id"]
    commit_args = ["workspace", "commit", data["id"], "--repo", str(tmp_path), "--plan-id", plan_id]
    pending = runner.invoke(app, commit_args)
    assert pending.exit_code == 3, pending.output
    ApprovalService(store).decide_current(
        json.loads(pending.output)["approval_id"], approved=True, source="test"
    )
    committed = runner.invoke(app, commit_args)
    assert committed.exit_code == 0, committed.output
    assert (tmp_path / "tracked").read_bytes() == b"source\n"
    assert (tmp_path / ".git" / "index").read_bytes() == source_index
    args = ["workspace", "close", data["id"], "--repo", str(tmp_path)]
    with pytest.raises(ValueError, match="approve workspace close"):
        SessionService(store).close(session.id)
    assert target.is_dir()
    assert store.get_workspace(data["id"]).cleanup_status == "approval_required"
    pending = runner.invoke(app, args)
    assert pending.exit_code == 3, pending.output
    ApprovalService(store).decide_current(
        json.loads(pending.output)["approval_id"], approved=True, source="test"
    )
    closed = runner.invoke(app, args)
    assert closed.exit_code == 0, closed.output
    assert not target.exists()


def test_runtime_lazily_binds_workspace_and_records_effect(tmp_path: Path) -> None:
    from patchloop.domain import Task, ToolCall
    from patchloop.providers import FakeProvider, ModelResponse
    from patchloop.runtime import AgentRuntime
    from patchloop.tools import PermissionLevel, ToolContext, ToolGateway, ToolPolicy, WriteFileTool

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "agent.txt").write_bytes(b"baseline\n")
    (repo / "user.txt").write_bytes(b"user baseline\n")
    for args in (
        ["init"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.test"],
        ["add", "."],
        ["commit", "-m", "baseline"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "user.txt").write_bytes(b"preexisting dirty\r\n")
    store = SQLiteStore(tmp_path / "state.db")
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        name="write_file",
                        arguments={"path": "agent.txt", "content": "agent update\n"},
                    )
                ]
            ),
            ModelResponse(content="Done"),
        ]
    )
    context = ToolContext(repo)
    gateway = ToolGateway(
        context,
        [WriteFileTool()],
        policy=ToolPolicy(
            frozenset({PermissionLevel.READ, PermissionLevel.WRITE}),
            require_plan_for_mutations=False,
        ),
    )
    task = AgentRuntime(provider, gateway, state_store=store).run(
        Task(goal="update the agent file", repository=str(repo))
    )
    handles = store.list_workspaces(task.session_id)
    assert len(handles) == 1 and handles[0].legacy_direct
    records = store.list_changes(handles[0].id)
    agent = next(item for item in records if item.path == "agent.txt")
    assert agent.ownership == "agent"
    assert agent.effect_ids[0] in {effect.id for effect in store.list_effects(task.id)}
    assert (repo / "user.txt").read_bytes() == b"preexisting dirty\r\n"
