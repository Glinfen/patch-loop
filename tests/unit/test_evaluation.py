from collections import Counter
from pathlib import Path
from threading import Lock

import pytest

from patchloop.evaluation import (
    Difficulty,
    EvaluationCandidate,
    EvaluationManifest,
    EvaluationRunner,
    EvaluationTask,
    EvaluationVariant,
    RepositoryDefinition,
    SuccessCriteria,
    TaskType,
    load_evaluation_manifest,
    repository_tree_sha256,
)
from patchloop.evaluation.runner import RepositoryRevisionError


def test_week08_manifest_has_fixed_balanced_task_protocol() -> None:
    root = Path(__file__).parents[2]
    manifest = load_evaluation_manifest(root / "benchmarks" / "evaluation_manifest.json")
    repository = root / manifest.repositories["service-fixture"].path

    assert len(manifest.tasks) == 30
    assert Counter(task.difficulty for task in manifest.tasks) == {
        Difficulty.EASY: 10,
        Difficulty.MEDIUM: 10,
        Difficulty.HARD: 10,
    }
    assert Counter(task.task_type for task in manifest.tasks) == {
        TaskType.BUGFIX: 8,
        TaskType.FEATURE: 8,
        TaskType.TEST: 8,
        TaskType.DOCUMENTATION: 6,
    }
    assert (
        repository_tree_sha256(repository) == manifest.repositories["service-fixture"].tree_sha256
    )


class FlakyExecutor:
    variant = EvaluationVariant.PATCHLOOP

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.lock = Lock()

    def execute(self, task: EvaluationTask, repository: Path) -> EvaluationCandidate:
        with self.lock:
            self.calls[task.id] += 1
            attempt = self.calls[task.id]
        if attempt == 1:
            raise RuntimeError("transient worker failure")
        return EvaluationCandidate(
            selected_paths=task.criteria.expected_paths, plan_steps=["verify"]
        )


def test_runner_retries_concurrently_and_preserves_manifest_order(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "target.py").write_text("VALUE = 1\n", encoding="utf-8")
    tasks = [
        EvaluationTask(
            id=f"task-{index}",
            repository="fixture",
            goal="Locate target",
            query="target value",
            task_type=TaskType.BUGFIX,
            difficulty=Difficulty.EASY,
            criteria=SuccessCriteria(expected_paths=["target.py"]),
        )
        for index in range(4)
    ]
    manifest = EvaluationManifest(
        suite_id="retry-suite",
        revision="v1",
        repositories={
            "fixture": RepositoryDefinition(
                path="repository",
                revision="fixture-v1",
                tree_sha256=repository_tree_sha256(repository),
            )
        },
        tasks=tasks,
    )

    report = EvaluationRunner(tmp_path, jobs=4, retries=1).run(manifest, FlakyExecutor())

    assert report.aggregate.passed == 4
    assert report.aggregate.total_attempts == 8
    assert [result.task_id for result in report.results] == [task.id for task in tasks]
    assert all(result.attempts[0].error for result in report.results)
    assert all(result.attempts[1].passed for result in report.results)


def test_runner_rejects_repository_revision_drift(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "target.py").write_text("VALUE = 1\n", encoding="utf-8")
    task = EvaluationTask(
        id="drift-task",
        repository="fixture",
        goal="Locate target",
        query="target",
        task_type=TaskType.BUGFIX,
        difficulty=Difficulty.EASY,
        criteria=SuccessCriteria(expected_paths=["target.py"]),
    )
    manifest = EvaluationManifest(
        suite_id="drift-suite",
        revision="v1",
        repositories={
            "fixture": RepositoryDefinition(
                path="repository",
                revision="fixture-v1",
                tree_sha256="0" * 64,
            )
        },
        tasks=[task],
    )

    with pytest.raises(RepositoryRevisionError, match="fingerprint mismatch"):
        EvaluationRunner(tmp_path).run(manifest, FlakyExecutor())
