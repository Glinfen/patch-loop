"""Deterministic baselines sharing the evaluation success protocol."""

from __future__ import annotations

from pathlib import Path
from threading import Lock

from patchloop.evaluation.models import (
    EvaluationCandidate,
    EvaluationTask,
    EvaluationVariant,
    TaskType,
)
from patchloop.intelligence import RepositoryIndexer, RepositorySearch
from patchloop.intelligence.search import tokenize


class RetrievalBaseline:
    def __init__(self, variant: EvaluationVariant) -> None:
        self.variant = variant
        self._searchers: dict[str, RepositorySearch] = {}
        self._lock = Lock()

    def execute(self, task: EvaluationTask, repository: Path) -> EvaluationCandidate:
        searcher = self._searcher(repository)
        if self.variant is EvaluationVariant.SINGLE_SHOT:
            mode, limit, plan = "text", 1, []
        elif self.variant is EvaluationVariant.TEXT_ONLY:
            mode, limit, plan = "text", task.k, ["retrieve text matches"]
        elif self.variant is EvaluationVariant.NO_PLAN:
            mode, limit, plan = "hybrid", task.k, []
        else:
            mode, limit, plan = (
                "hybrid",
                task.k,
                ["retrieve ranked repository evidence", "verify evidence against success criteria"],
            )
        search_limit = 50 if self.variant is EvaluationVariant.PATCHLOOP else limit
        hits = searcher.search(task.query, limit=search_limit, mode=mode)
        if self.variant is EvaluationVariant.PATCHLOOP:
            hits.sort(key=lambda hit: not _matches_task_type(hit.path, task.task_type))
            hits = hits[:limit]
        selected_paths = [hit.path for hit in hits]
        if self.variant is EvaluationVariant.PATCHLOOP and task.task_type is TaskType.DOCUMENTATION:
            selected_paths = _documentation_paths(repository, task.query) + selected_paths
            selected_paths = list(dict.fromkeys(selected_paths))[:limit]
        return EvaluationCandidate(
            selected_paths=selected_paths,
            plan_steps=plan,
            notes=f"{self.variant} used {mode} retrieval",
            input_tokens=_estimate_tokens(task.query),
            output_tokens=_estimate_tokens("\n".join([*selected_paths, *plan])),
        )

    def _searcher(self, repository: Path) -> RepositorySearch:
        key = str(repository)
        with self._lock:
            searcher = self._searchers.get(key)
            if searcher is None:
                snapshot = RepositoryIndexer(repository).build()
                searcher = RepositorySearch(repository, snapshot)
                self._searchers[key] = searcher
            return searcher


def _matches_task_type(path: str, task_type: TaskType) -> bool:
    normalized = path.casefold()
    if task_type is TaskType.DOCUMENTATION:
        return normalized.endswith((".md", ".rst", ".txt"))
    if task_type is TaskType.TEST:
        return normalized.startswith("tests/") or "/test" in normalized
    return not normalized.startswith("tests/") and not normalized.endswith(".md")


def _documentation_paths(repository: Path, query: str) -> list[str]:
    query_tokens = set(tokenize(query))
    ranked = []
    for path in repository.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in {".md", ".rst", ".txt"}:
            continue
        relative = path.relative_to(repository).as_posix()
        if any(part.startswith(".") for part in path.relative_to(repository).parts):
            continue
        content_tokens = set(tokenize(path.read_text(encoding="utf-8", errors="replace")))
        ranked.append((len(query_tokens & content_tokens), relative))
    return [path for score, path in sorted(ranked, key=lambda item: (-item[0], item[1])) if score]


def _estimate_tokens(value: str) -> int:
    return (len(value.encode("utf-8")) + 2) // 3
