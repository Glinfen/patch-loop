"""Concurrent evaluation execution, retry, judging, and aggregation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import perf_counter
from typing import Protocol

from patchloop.evaluation.manifest import repository_tree_sha256
from patchloop.evaluation.models import (
    Difficulty,
    EvaluationAggregate,
    EvaluationAttempt,
    EvaluationCandidate,
    EvaluationManifest,
    EvaluationReport,
    EvaluationSuiteReport,
    EvaluationTask,
    EvaluationTaskResult,
    EvaluationVariant,
    TaskType,
)


class EvaluationExecutor(Protocol):
    variant: EvaluationVariant

    def execute(self, task: EvaluationTask, repository: Path) -> EvaluationCandidate: ...


class RepositoryRevisionError(ValueError):
    pass


class EvaluationRunner:
    def __init__(self, root: Path, *, jobs: int = 1, retries: int = 0) -> None:
        if jobs < 1:
            raise ValueError("evaluation jobs must be positive")
        if retries < 0:
            raise ValueError("evaluation retries cannot be negative")
        self.root = root.resolve(strict=True)
        self.jobs = jobs
        self.retries = retries

    def run(
        self,
        manifest: EvaluationManifest,
        executor: EvaluationExecutor,
    ) -> EvaluationReport:
        repositories = self._verify_repositories(manifest)
        indexed_results: dict[int, EvaluationTaskResult] = {}
        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            futures = {
                pool.submit(self._run_task, task, repositories[task.repository], executor): index
                for index, task in enumerate(manifest.tasks)
            }
            for future in as_completed(futures):
                indexed_results[futures[future]] = future.result()
        results = [indexed_results[index] for index in range(len(manifest.tasks))]
        return EvaluationReport(
            suite_id=manifest.suite_id,
            suite_revision=manifest.revision,
            variant=executor.variant,
            repository_revisions={
                name: definition.revision
                for name, definition in sorted(manifest.repositories.items())
            },
            aggregate=_aggregate(results),
            results=results,
        )

    def run_suite(
        self,
        manifest: EvaluationManifest,
        executors: Iterable[EvaluationExecutor],
    ) -> EvaluationSuiteReport:
        reports = [self.run(manifest, executor) for executor in executors]
        return EvaluationSuiteReport(
            suite_id=manifest.suite_id,
            suite_revision=manifest.revision,
            reports=reports,
        )

    def _verify_repositories(self, manifest: EvaluationManifest) -> dict[str, Path]:
        repositories: dict[str, Path] = {}
        for name, definition in manifest.repositories.items():
            repository = (self.root / definition.path).resolve(strict=True)
            try:
                repository.relative_to(self.root)
            except ValueError as exc:
                raise RepositoryRevisionError(
                    f"repository {name} escapes evaluation root: {definition.path}"
                ) from exc
            actual = repository_tree_sha256(repository)
            if actual != definition.tree_sha256:
                raise RepositoryRevisionError(
                    f"repository {name} fingerprint mismatch: expected "
                    f"{definition.tree_sha256}, got {actual}"
                )
            repositories[name] = repository
        return repositories

    def _run_task(
        self,
        task: EvaluationTask,
        repository: Path,
        executor: EvaluationExecutor,
    ) -> EvaluationTaskResult:
        attempts = []
        for attempt_index in range(1, self.retries + 2):
            started = perf_counter()
            try:
                candidate = executor.execute(task, repository)
                expected = set(task.criteria.expected_paths)
                matched = expected.intersection(candidate.selected_paths)
                recall = len(matched) / len(expected)
                passed = recall >= task.criteria.minimum_path_recall
                error = None
            except Exception as exc:
                candidate = EvaluationCandidate()
                recall = 0.0
                passed = False
                error = f"{type(exc).__name__}: {exc}"
            attempts.append(
                EvaluationAttempt(
                    attempt=attempt_index,
                    passed=passed,
                    path_recall=recall,
                    selected_paths=candidate.selected_paths,
                    plan_steps=len(candidate.plan_steps),
                    duration_ms=(perf_counter() - started) * 1_000,
                    error=error,
                )
            )
            if passed:
                break
        return EvaluationTaskResult(
            task_id=task.id,
            task_type=task.task_type,
            difficulty=task.difficulty,
            passed=attempts[-1].passed,
            attempts=attempts,
        )


def _aggregate(results: list[EvaluationTaskResult]) -> EvaluationAggregate:
    passed = sum(result.passed for result in results)
    difficulty_groups: dict[Difficulty, list[bool]] = defaultdict(list)
    type_groups: dict[TaskType, list[bool]] = defaultdict(list)
    for result in results:
        difficulty_groups[result.difficulty].append(result.passed)
        type_groups[result.task_type].append(result.passed)
    return EvaluationAggregate(
        total=len(results),
        passed=passed,
        success_rate=passed / len(results) if results else 0.0,
        total_attempts=sum(len(result.attempts) for result in results),
        by_difficulty={
            difficulty.value: sum(values) / len(values)
            for difficulty, values in sorted(difficulty_groups.items())
        },
        by_task_type={
            task_type.value: sum(values) / len(values)
            for task_type, values in sorted(type_groups.items())
        },
    )
