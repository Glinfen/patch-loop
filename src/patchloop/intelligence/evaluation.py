"""Fixed-task Recall@K evaluation for repository retrieval."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from patchloop.intelligence.index import RepositoryIndexer
from patchloop.intelligence.models import (
    RetrievalEvaluationReport,
    RetrievalTask,
    RetrievalTaskResult,
)
from patchloop.intelligence.search import RepositorySearch


def load_retrieval_tasks(path: Path) -> list[RetrievalTask]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return TypeAdapter(list[RetrievalTask]).validate_python(payload)


def evaluate_retrieval(tasks: list[RetrievalTask], root: Path) -> RetrievalEvaluationReport:
    if not tasks:
        raise ValueError("retrieval task set must not be empty")
    root = root.resolve(strict=True)
    results: list[RetrievalTaskResult] = []
    searchers: dict[str, RepositorySearch] = {}
    for task in tasks:
        repository = (root / task.repository).resolve(strict=True)
        repository.relative_to(root)
        cache_key = str(repository)
        if cache_key not in searchers:
            snapshot = RepositoryIndexer(repository).build()
            searchers[cache_key] = RepositorySearch(repository, snapshot)
        searcher = searchers[cache_key]
        baseline_paths = [
            hit.path for hit in searcher.search(task.query, limit=task.k, mode="text")
        ]
        hybrid_paths = [
            hit.path for hit in searcher.search(task.query, limit=task.k, mode="hybrid")
        ]
        expected = set(task.expected_paths)
        results.append(
            RetrievalTaskResult(
                task_id=task.id,
                query=task.query,
                expected_paths=task.expected_paths,
                baseline_paths=baseline_paths,
                hybrid_paths=hybrid_paths,
                baseline_hit=bool(expected & set(baseline_paths)),
                hybrid_hit=bool(expected & set(hybrid_paths)),
            )
        )
    baseline_recall = sum(result.baseline_hit for result in results) / len(results)
    hybrid_recall = sum(result.hybrid_hit for result in results) / len(results)
    return RetrievalEvaluationReport(
        task_count=len(results),
        baseline_recall_at_k=baseline_recall,
        hybrid_recall_at_k=hybrid_recall,
        improvement=hybrid_recall - baseline_recall,
        results=results,
    )
