import json
from pathlib import Path

from typer.testing import CliRunner

from patchloop.cli import app

runner = CliRunner()


def test_cli_creates_and_reads_task(tmp_path: Path) -> None:
    created = runner.invoke(app, ["task", "create", "Fix the bug", "--repo", str(tmp_path)])

    assert created.exit_code == 0, created.output
    task_id = json.loads(created.output)["id"]

    shown = runner.invoke(app, ["task", "show", task_id, "--repo", str(tmp_path)])

    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["goal"] == "Fix the bug"
