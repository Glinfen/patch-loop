import json
from pathlib import Path

from typer.testing import CliRunner

from patchloop.cli import app
from patchloop.domain import Task, TaskExecutionConfig, TaskStatus
from patchloop.persistence import RuntimeCheckpoint, SQLiteStore
from patchloop.providers import FakeProvider, ModelMessage, ModelResponse

runner = CliRunner()


def test_cli_creates_and_reads_task(tmp_path: Path) -> None:
    created = runner.invoke(app, ["task", "create", "Fix the bug", "--repo", str(tmp_path)])

    assert created.exit_code == 0, created.output
    task_id = json.loads(created.output)["id"]

    shown = runner.invoke(app, ["task", "show", task_id, "--repo", str(tmp_path)])

    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["goal"] == "Fix the bug"
    assert (tmp_path / ".patchloop" / "patchloop.db").is_file()


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
    assert (tmp_path / ".patchloop" / "patchloop.db").is_file()
    assert (tmp_path / ".patchloop" / "artifacts" / payload["id"] / "report.json").is_file()

    status = runner.invoke(app, ["status", payload["id"], "--repo", str(tmp_path)])
    diff = runner.invoke(app, ["diff", payload["id"], "--repo", str(tmp_path)])

    assert status.exit_code == 0
    assert json.loads(status.output)["status"] == "completed"
    assert diff.exit_code == 0 and "No changes." in diff.output


def test_cli_cancels_created_task(tmp_path: Path) -> None:
    created = runner.invoke(app, ["task", "create", "Cancel me", "--repo", str(tmp_path)])
    task_id = json.loads(created.output)["id"]

    cancelled = runner.invoke(app, ["cancel", task_id, "--repo", str(tmp_path)])

    assert cancelled.exit_code == 0
    assert json.loads(cancelled.output)["status"] == "cancelled"


def test_cli_resumes_running_task(tmp_path: Path, monkeypatch: object) -> None:
    task = Task(
        goal="Resume me",
        repository=str(tmp_path),
        execution=TaskExecutionConfig(allowed_permissions=["read"]),
    )
    task.transition(TaskStatus.RUNNING)
    store = SQLiteStore(tmp_path / ".patchloop" / "patchloop.db")
    store.save_task(task)
    store.save_checkpoint(
        RuntimeCheckpoint(
            task_id=task.id,
            next_step_index=0,
            messages=[
                ModelMessage(role="system", content="system"),
                ModelMessage(role="user", content=task.goal),
            ],
        )
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "patchloop.cli.DeepSeekProvider.from_env",
        lambda: FakeProvider([ModelResponse(content="Resumed successfully.")]),
    )

    result = runner.invoke(app, ["resume", task.id, "--repo", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "completed"
    assert store.get_task(task.id).status is TaskStatus.COMPLETED
