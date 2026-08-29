"""Deterministic ablation, failure analysis, optimization, and stability runs."""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter
from pathlib import Path
from threading import Lock
from time import perf_counter

from pydantic import BaseModel, ConfigDict, Field

from patchloop.evaluation.baselines import _documentation_paths, _estimate_tokens
from patchloop.evaluation.models import (
    EvaluationCandidate,
    EvaluationManifest,
    EvaluationReport,
    EvaluationTask,
    EvaluationVariant,
    TaskType,
)
from patchloop.evaluation.runner import EvaluationRunner
from patchloop.intelligence import RepositoryIndexer, RepositorySearch


class ExperimentFeatures(BaseModel):
    model_config = ConfigDict(frozen=True)

    planning: bool = True
    retrieval: bool = True
    hybrid_retrieval: bool = True
    reflection: bool = True
    task_routing: bool = True
    top_k: int | None = Field(default=None, ge=1, le=20)


class ExperimentConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    role: str
    features: ExperimentFeatures


class ExperimentRunSummary(BaseModel):
    repeat: int = Field(ge=1)
    passed: int = Field(ge=0)
    total: int = Field(ge=0)
    success_rate: float = Field(ge=0.0, le=1.0)
    mean_task_latency_ms: float = Field(ge=0.0)
    run_elapsed_ms: float = Field(ge=0.0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    result_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExperimentVariantSummary(BaseModel):
    name: str
    role: str
    features: ExperimentFeatures
    runs: list[ExperimentRunSummary]
    mean_success_rate: float = Field(ge=0.0, le=1.0)
    min_success_rate: float = Field(ge=0.0, le=1.0)
    max_success_rate: float = Field(ge=0.0, le=1.0)
    success_rate_stddev: float = Field(ge=0.0)
    deterministic_results: bool
    delta_vs_full: float = Field(ge=-1.0, le=1.0)
    mean_task_latency_ms: float = Field(ge=0.0)
    mean_run_elapsed_ms: float = Field(ge=0.0)
    total_cost_usd: float = Field(ge=0.0)
    failures: dict[str, int] = Field(default_factory=dict)
    failure_examples: dict[str, list[str]] = Field(default_factory=dict)


class OptimizationStage(BaseModel):
    name: str
    passed: int = Field(ge=0)
    total: int = Field(ge=0)
    success_rate: float = Field(ge=0.0, le=1.0)
    improvement_vs_baseline: float = Field(ge=-1.0, le=1.0)


class ExperimentReport(BaseModel):
    suite_id: str
    suite_revision: str
    repeats: int = Field(ge=1)
    jobs: int = Field(ge=1)
    variants: list[ExperimentVariantSummary]
    optimization_stages: list[OptimizationStage]
    largest_baseline_failures: dict[str, int]
    conclusions: list[str]


DEFAULT_EXPERIMENTS = [
    ExperimentConfig(
        name="single-shot-baseline",
        role="optimization_baseline",
        features=ExperimentFeatures(
            planning=False,
            hybrid_retrieval=False,
            reflection=False,
            task_routing=False,
            top_k=1,
        ),
    ),
    ExperimentConfig(
        name="routed-text",
        role="optimization_stage",
        features=ExperimentFeatures(
            planning=False,
            hybrid_retrieval=False,
            reflection=False,
            task_routing=True,
        ),
    ),
    ExperimentConfig(name="full", role="system", features=ExperimentFeatures()),
    ExperimentConfig(
        name="no-planning",
        role="ablation",
        features=ExperimentFeatures(planning=False),
    ),
    ExperimentConfig(
        name="no-retrieval",
        role="ablation",
        features=ExperimentFeatures(retrieval=False),
    ),
    ExperimentConfig(
        name="no-reflection",
        role="ablation",
        features=ExperimentFeatures(reflection=False),
    ),
]


class AblationExecutor:
    variant = EvaluationVariant.PATCHLOOP

    def __init__(self, config: ExperimentConfig) -> None:
        self.config = config
        self._searchers: dict[str, RepositorySearch] = {}
        self._lock = Lock()

    def execute(self, task: EvaluationTask, repository: Path) -> EvaluationCandidate:
        features = self.config.features
        limit = features.top_k or task.k
        if features.retrieval:
            search_limit = 50 if features.task_routing else limit
            mode = "hybrid" if features.hybrid_retrieval else "text"
            hits = self._searcher(repository).search(task.query, limit=search_limit, mode=mode)
            selected = [hit.path for hit in hits]
        else:
            mode = "disabled"
            selected = sorted(
                path.relative_to(repository).as_posix()
                for path in repository.rglob("*.py")
                if path.is_file() and "__pycache__" not in path.parts
            )
        if features.task_routing:
            selected.sort(key=lambda path: not _matches_task_type(path, task.task_type))
        selected = selected[:limit]
        if features.reflection and task.task_type is TaskType.DOCUMENTATION:
            selected = _documentation_paths(repository, task.query) + selected
            selected = list(dict.fromkeys(selected))[:limit]
        plan = (
            ["retrieve task evidence", "verify candidate type and coverage"]
            if features.planning
            else []
        )
        return EvaluationCandidate(
            selected_paths=selected,
            plan_steps=plan,
            notes=f"{self.config.name}: retrieval={mode}",
            input_tokens=_estimate_tokens(task.query),
            output_tokens=_estimate_tokens("\n".join([*selected, *plan])),
        )

    def _searcher(self, repository: Path) -> RepositorySearch:
        key = str(repository)
        with self._lock:
            searcher = self._searchers.get(key)
            if searcher is None:
                searcher = RepositorySearch(repository, RepositoryIndexer(repository).build())
                self._searchers[key] = searcher
            return searcher


class ExperimentRunner:
    def __init__(
        self,
        root: Path,
        *,
        jobs: int = 4,
        retries: int = 0,
        repeats: int = 5,
    ) -> None:
        if repeats < 1:
            raise ValueError("experiment repeats must be positive")
        self.runner = EvaluationRunner(root, jobs=jobs, retries=retries)
        self.jobs = jobs
        self.repeats = repeats

    def run(
        self,
        manifest: EvaluationManifest,
        configs: list[ExperimentConfig] | None = None,
    ) -> ExperimentReport:
        selected_configs = DEFAULT_EXPERIMENTS if configs is None else configs
        names = [config.name for config in selected_configs]
        if len(names) != len(set(names)):
            raise ValueError("experiment config names must be unique")
        required = {"single-shot-baseline", "routed-text", "full"}
        if missing := sorted(required - set(names)):
            raise ValueError(f"missing required experiment configs: {', '.join(missing)}")
        reports_by_name: dict[str, list[EvaluationReport]] = {}
        run_summaries: dict[str, list[ExperimentRunSummary]] = {}
        for config in selected_configs:
            reports = []
            summaries = []
            for repeat in range(1, self.repeats + 1):
                started = perf_counter()
                report = self.runner.run(manifest, AblationExecutor(config))
                elapsed_ms = (perf_counter() - started) * 1_000
                reports.append(report)
                summaries.append(_run_summary(repeat, report, elapsed_ms))
            reports_by_name[config.name] = reports
            run_summaries[config.name] = summaries
        full_rate = _mean_rate(run_summaries["full"])
        variants = [
            _variant_summary(
                config,
                reports_by_name[config.name][0],
                run_summaries[config.name],
                full_rate,
                manifest,
            )
            for config in selected_configs
        ]
        by_name = {variant.name: variant for variant in variants}
        baseline_rate = by_name["single-shot-baseline"].mean_success_rate
        stages = [
            OptimizationStage(
                name=name,
                passed=reports_by_name[name][0].aggregate.passed,
                total=reports_by_name[name][0].aggregate.total,
                success_rate=by_name[name].mean_success_rate,
                improvement_vs_baseline=round(
                    by_name[name].mean_success_rate - baseline_rate,
                    6,
                ),
            )
            for name in ("single-shot-baseline", "routed-text", "full")
        ]
        baseline_failures = by_name["single-shot-baseline"].failures
        return ExperimentReport(
            suite_id=manifest.suite_id,
            suite_revision=manifest.revision,
            repeats=self.repeats,
            jobs=self.jobs,
            variants=variants,
            optimization_stages=stages,
            largest_baseline_failures=dict(
                sorted(baseline_failures.items(), key=lambda item: (-item[1], item[0]))[:2]
            ),
            conclusions=_conclusions(by_name),
        )


def _matches_task_type(path: str, task_type: TaskType) -> bool:
    normalized = path.casefold()
    if task_type is TaskType.DOCUMENTATION:
        return normalized.endswith((".md", ".rst", ".txt"))
    if task_type is TaskType.TEST:
        return normalized.startswith("tests/") or "/test" in normalized
    return not normalized.startswith("tests/") and not normalized.endswith(".md")


def _run_summary(
    repeat: int,
    report: EvaluationReport,
    elapsed_ms: float,
) -> ExperimentRunSummary:
    fingerprint_payload = [
        {
            "task_id": result.task_id,
            "passed": result.passed,
            "selected_paths": result.attempts[-1].selected_paths,
        }
        for result in report.results
    ]
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return ExperimentRunSummary(
        repeat=repeat,
        passed=report.aggregate.passed,
        total=report.aggregate.total,
        success_rate=report.aggregate.success_rate,
        mean_task_latency_ms=report.aggregate.mean_latency_ms,
        run_elapsed_ms=elapsed_ms,
        input_tokens=report.aggregate.input_tokens,
        output_tokens=report.aggregate.output_tokens,
        cost_usd=report.aggregate.total_cost_usd,
        result_fingerprint=fingerprint,
    )


def _mean_rate(runs: list[ExperimentRunSummary]) -> float:
    return statistics.mean(run.success_rate for run in runs)


def _variant_summary(
    config: ExperimentConfig,
    report: EvaluationReport,
    runs: list[ExperimentRunSummary],
    full_rate: float,
    manifest: EvaluationManifest,
) -> ExperimentVariantSummary:
    rates = [run.success_rate for run in runs]
    failures, examples = _failure_distribution(report, manifest)
    return ExperimentVariantSummary(
        name=config.name,
        role=config.role,
        features=config.features,
        runs=runs,
        mean_success_rate=statistics.mean(rates),
        min_success_rate=min(rates),
        max_success_rate=max(rates),
        success_rate_stddev=statistics.pstdev(rates),
        deterministic_results=len({run.result_fingerprint for run in runs}) == 1,
        delta_vs_full=round(statistics.mean(rates) - full_rate, 6),
        mean_task_latency_ms=statistics.mean(run.mean_task_latency_ms for run in runs),
        mean_run_elapsed_ms=statistics.mean(run.run_elapsed_ms for run in runs),
        total_cost_usd=sum(run.cost_usd for run in runs),
        failures=failures,
        failure_examples=examples,
    )


def _failure_distribution(
    report: EvaluationReport,
    manifest: EvaluationManifest,
) -> tuple[dict[str, int], dict[str, list[str]]]:
    tasks = {task.id: task for task in manifest.tasks}
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    for result in report.results:
        if result.passed:
            continue
        task = tasks[result.task_id]
        attempt = result.attempts[-1]
        if attempt.error:
            category = "executor_error"
        elif task.task_type is TaskType.DOCUMENTATION:
            category = "documentation_not_retrieved"
        elif task.task_type is TaskType.TEST and not any(
            _matches_task_type(path, TaskType.TEST) for path in attempt.selected_paths
        ):
            category = "test_source_confusion"
        elif not attempt.selected_paths:
            category = "no_candidates"
        else:
            category = "expected_path_not_retrieved"
        counts[category] += 1
        examples.setdefault(category, []).append(task.id)
    return dict(sorted(counts.items())), {
        category: task_ids[:5] for category, task_ids in sorted(examples.items())
    }


def _conclusions(by_name: dict[str, ExperimentVariantSummary]) -> list[str]:
    full = by_name["full"]
    return [
        "Planning changed localization success by "
        f"{full.mean_success_rate - by_name['no-planning'].mean_success_rate:+.3f}.",
        "Removing retrieval changed localization success by "
        f"{by_name['no-retrieval'].mean_success_rate - full.mean_success_rate:+.3f}.",
        "Removing reflection changed localization success by "
        f"{by_name['no-reflection'].mean_success_rate - full.mean_success_rate:+.3f}.",
        "All repeated variants produced stable task-result fingerprints."
        if all(item.deterministic_results for item in by_name.values())
        else "At least one variant produced unstable task-result fingerprints.",
    ]
