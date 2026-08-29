from pathlib import Path

from patchloop.domain import TaskStatus, ToolCall
from patchloop.evaluation import (
    CodingBenchmarkRunner,
    CodingTaskManifest,
    load_coding_manifest,
    repository_tree_sha256,
)
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.sandbox import LocalProcessSandbox


def test_published_coding_tasks_are_locked_and_start_with_failing_tests() -> None:
    root = Path(__file__).parents[2]
    manifest = load_coding_manifest(root / "benchmarks" / "coding_tasks.json")
    sandbox = LocalProcessSandbox()

    assert len(manifest.tasks) == 6
    for task in manifest.tasks:
        fixture = root / task.fixture
        assert repository_tree_sha256(fixture) == task.fixture_sha256
        assert not set(task.expected_changed_files) & set(task.forbidden_changed_files)
        result = sandbox.execute(
            task.test_command,
            fixture,
            timeout_seconds=30.0,
            max_output_chars=2_000,
        )
        assert result.exit_code != 0, f"{task.id} no longer starts from a failing state"


def _repair_provider(*, add_extra_file: bool = False) -> FakeProvider:
    responses = [
        ModelResponse(tool_calls=[ToolCall(name="read_file", arguments={"path": "calculator.py"})]),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    name="update_plan",
                    arguments={
                        "items": [
                            {"description": "Inspect defect", "status": "completed"},
                            {"description": "Fix division", "status": "running"},
                            {"description": "Verify tests", "status": "pending"},
                        ]
                    },
                )
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    name="apply_patch",
                    arguments={
                        "path": "calculator.py",
                        "edits": [
                            {
                                "old_text": "return dividend // divisor",
                                "new_text": "return dividend / divisor",
                            }
                        ],
                    },
                )
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    name="run_tests",
                    arguments={"command": ["python", "-m", "pytest", "-q"]},
                )
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    name="update_plan",
                    arguments={
                        "items": [
                            {"description": "Inspect defect", "status": "completed"},
                            {"description": "Fix division", "status": "completed"},
                            {"description": "Verify tests", "status": "completed"},
                        ]
                    },
                )
            ]
        ),
        ModelResponse(content="Fixed division and verified the regression suite."),
    ]
    if add_extra_file:
        responses.insert(
            3,
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        name="create_file",
                        arguments={"path": "answer.txt", "content": "tests pass"},
                    )
                ]
            ),
        )
    return FakeProvider(responses)


def _calculator_manifest(root: Path) -> CodingTaskManifest:
    fixture = root / "benchmarks" / "fixtures" / "calculator_bug"
    return CodingTaskManifest.model_validate(
        {
            "suite_id": "test-coding",
            "revision": "test-1",
            "tasks": [
                {
                    "id": "calculator-division",
                    "fixture": "benchmarks/fixtures/calculator_bug",
                    "fixture_sha256": repository_tree_sha256(fixture),
                    "goal": "Fix calculator division without changing tests.",
                    "task_type": "bugfix",
                    "difficulty": "easy",
                    "test_command": ["python", "-m", "pytest", "-q"],
                    "expected_changed_files": ["calculator.py"],
                    "forbidden_changed_files": ["test_calculator.py"],
                }
            ],
        }
    )


def test_coding_benchmark_copies_runs_and_independently_verifies_task(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[2]
    fixture = root / "benchmarks" / "fixtures" / "calculator_bug"
    original = (fixture / "calculator.py").read_text(encoding="utf-8")
    manifest = _calculator_manifest(root)

    report = CodingBenchmarkRunner(
        root,
        tmp_path / "runs",
        _repair_provider,
        LocalProcessSandbox,
    ).run(manifest)

    assert report.total_runs == 1
    assert report.successful_runs == 1
    assert report.success_rate == 1.0
    result = report.results[0]
    assert result.agent_status is TaskStatus.COMPLETED
    assert result.tests_passed
    assert result.expected_files_changed
    assert result.only_allowed_files_changed
    assert result.changed_files == ["calculator.py"]
    assert result.verifier_exit_code == 0
    assert Path(result.trace_path).is_file()
    assert "dividend / divisor" in (Path(result.workspace) / "calculator.py").read_text(
        encoding="utf-8"
    )
    assert (fixture / "calculator.py").read_text(encoding="utf-8") == original


def test_coding_benchmark_rejects_unexpected_file_changes(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    report = CodingBenchmarkRunner(
        root,
        tmp_path / "runs",
        lambda: _repair_provider(add_extra_file=True),
        LocalProcessSandbox,
    ).run(_calculator_manifest(root))

    result = report.results[0]
    assert result.tests_passed
    assert result.agent_status is TaskStatus.COMPLETED
    assert not result.success
    assert not result.only_allowed_files_changed
    assert result.unexpected_changes == ["answer.txt"]
