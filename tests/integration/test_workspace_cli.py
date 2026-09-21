"""CLI JSON contracts and durable request/approval/retry lifecycle."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from patchloop.cli import app
from patchloop.execution.approvals import ApprovalService
from patchloop.persistence import SQLiteStore


def test_cli_open_review_close(tmp_path: Path) -> None:
    for args in (
        ["init"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.test"],
        ["commit", "--allow-empty", "-m", "baseline"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    runner = CliRunner()
    create = runner.invoke(app, ["session", "--repo", str(tmp_path), "create"])
    assert create.exit_code == 0, create.output
    store = SQLiteStore(tmp_path / ".patchloop" / "patchloop.db")
    session = store.list_sessions()[0]
    opened = runner.invoke(app, ["workspace", "open", session.id, "--repo", str(tmp_path)])
    assert opened.exit_code == 0, opened.output
    workspace_id = json.loads(opened.output)["data"]["id"]
    reviewed = runner.invoke(app, ["workspace", "diff", workspace_id, "--repo", str(tmp_path)])
    assert reviewed.exit_code == 0, reviewed.output
    assert json.loads(reviewed.output)["data"]["paths"] == []
    close_args = ["workspace", "close", workspace_id, "--repo", str(tmp_path)]
    pending = runner.invoke(app, close_args)
    assert pending.exit_code == 3, pending.output
    approval_id = json.loads(pending.output)["approval_id"]
    ApprovalService(store).decide_current(approval_id, approved=True, source="test")
    closed = runner.invoke(app, close_args)
    assert closed.exit_code == 0, closed.output
    assert runner.invoke(app, close_args).exit_code == 0
    status = runner.invoke(app, ["workspace", "status", workspace_id, "--repo", str(tmp_path)])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["data"]["status"] == "closed"


def test_cli_unknown_workspace_is_structured(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["workspace", "show", "unknown", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "KeyError"
