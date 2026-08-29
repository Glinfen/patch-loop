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


def test_code_benchmark_requires_environment_key_without_creating_run(
    tmp_path: Path, monkeypatch: object
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)  # type: ignore[attr-defined]
    root = Path(__file__).parents[2]
    work_root = tmp_path / "runs"

    result = runner.invoke(
        app,
        [
            "benchmark-code",
            "--manifest",
            str(root / "benchmarks" / "coding_tasks.json"),
            "--root",
            str(root),
            "--work-root",
            str(work_root),
            "--output",
            str(tmp_path / "report.json"),
            "--sandbox",
            "local",
        ],
    )

    assert result.exit_code == 2
    assert "DEEPSEEK_API_KEY is not set" in result.output
    assert not work_root.exists()


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
    context = runner.invoke(app, ["context", payload["id"], "--repo", str(tmp_path)])
    metrics = runner.invoke(app, ["metrics", payload["id"], "--repo", str(tmp_path)])
    replay = runner.invoke(app, ["replay", payload["id"], "--repo", str(tmp_path)])

    assert status.exit_code == 0
    assert json.loads(status.output)["status"] == "completed"
    assert diff.exit_code == 0 and "No changes." in diff.output
    assert context.exit_code == 0
    assert "context [" in context.output
    assert metrics.exit_code == 0
    assert json.loads(metrics.output)["status"] == "completed"
    assert replay.exit_code == 0
    assert json.loads(replay.output)["frames"][-1]["type"] == "task.completed"


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


def test_cli_indexes_searches_and_benchmarks_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "math_service.py").write_text(
        "def divide(left: int, right: int) -> float:\n"
        '    """Return the quotient."""\n'
        "    return left / right\n",
        encoding="utf-8",
    )
    tasks = tmp_path / "retrieval.json"
    tasks.write_text(
        json.dumps(
            [
                {
                    "id": "locate-division",
                    "repository": "repository",
                    "query": "division implementation",
                    "expected_paths": ["math_service.py"],
                    "k": 1,
                }
            ]
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"

    indexed = runner.invoke(app, ["index", "--repo", str(repository)])
    searched = runner.invoke(
        app,
        ["search", "division implementation", "--repo", str(repository), "--limit", "1"],
    )
    benchmarked = runner.invoke(
        app,
        [
            "benchmark-search",
            "--tasks",
            str(tasks),
            "--root",
            str(tmp_path),
            "--output",
            str(report_path),
        ],
    )

    assert indexed.exit_code == 0, indexed.output
    assert json.loads(indexed.output)["symbols"] == 2
    assert (repository / ".patchloop" / "repository-index.json").is_file()
    assert searched.exit_code == 0, searched.output
    assert json.loads(searched.output)["results"][0]["path"] == "math_service.py"
    assert benchmarked.exit_code == 0, benchmarked.output
    assert json.loads(benchmarked.output)["hybrid_recall_at_k"] == 1.0
    assert report_path.is_file()
