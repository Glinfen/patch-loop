import json
from pathlib import Path

from typer.testing import CliRunner

from patchloop.cli import app
from patchloop.providers import FakeProvider, ModelResponse

runner = CliRunner()


def test_cli_creates_and_reads_task(tmp_path: Path) -> None:
    created = runner.invoke(app, ["task", "create", "Fix the bug", "--repo", str(tmp_path)])

    assert created.exit_code == 0, created.output
    task_id = json.loads(created.output)["id"]

    shown = runner.invoke(app, ["task", "show", task_id, "--repo", str(tmp_path)])

    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["goal"] == "Fix the bug"


def test_run_requires_environment_key(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)  # type: ignore[attr-defined]

    result = runner.invoke(app, ["run", "Inspect repository", "--repo", str(tmp_path)])

    assert result.exit_code == 2
    assert "DEEPSEEK_API_KEY is not set" in result.output


def test_run_uses_provider_and_persists_result(tmp_path: Path, monkeypatch: object) -> None:
    provider = FakeProvider([ModelResponse(content="Repository inspected.")])
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "patchloop.cli.DeepSeekProvider.from_env",
        lambda: provider,
    )

    result = runner.invoke(app, ["run", "Inspect repository", "--repo", str(tmp_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "completed"
    assert payload["result"] == "Repository inspected."
    assert (tmp_path / ".patchloop" / "tasks" / f"{payload['id']}.json").is_file()
    assert (tmp_path / ".patchloop" / "artifacts" / payload["id"] / "report.json").is_file()
