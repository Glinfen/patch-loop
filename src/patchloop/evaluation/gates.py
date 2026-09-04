"""PCO-07 acceptance gates, gray rollout policy, and recovery evidence."""

from __future__ import annotations

import statistics
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from patchloop.cache import CacheLayoutReason
from patchloop.domain import PromptCacheLayout
from patchloop.evaluation.cache import (
    CacheEvaluationReport,
    CacheEvaluationScenario,
    CacheEvaluationVariant,
    CacheRunReport,
)


class GateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"


class CacheGateCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    status: GateStatus
    actual: Any = None
    target: str
    detail: str


class MemoryQualityEvidence(BaseModel):
    """Candidate and baseline quality evidence required before cache rollout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    public_passed: int | None = Field(default=None, ge=0)
    public_total: int | None = Field(default=None, ge=1)
    hidden_passed: int | None = Field(default=None, ge=0)
    hidden_total: int | None = Field(default=None, ge=1)
    out_of_bounds_changes: int | None = Field(default=None, ge=0)
    secret_leaks: int | None = Field(default=None, ge=0)
    stale_fact_hits: int | None = Field(default=None, ge=0)
    candidate_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    baseline_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    candidate_critical_fact_recall: float | None = Field(default=None, ge=0.0, le=1.0)
    baseline_critical_fact_recall: float | None = Field(default=None, ge=0.0, le=1.0)
    candidate_stale_fact_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    baseline_stale_fact_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    candidate_repeated_failure_risk: float | None = Field(default=None, ge=0.0, le=1.0)
    baseline_repeated_failure_risk: float | None = Field(default=None, ge=0.0, le=1.0)
    candidate_recovery_consistency: float | None = Field(default=None, ge=0.0, le=1.0)
    baseline_recovery_consistency: float | None = Field(default=None, ge=0.0, le=1.0)


class CacheRolloutPolicy(BaseModel):
    """Explicit rollout switch with a one-release rollback path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    candidate_layout: PromptCacheLayout = PromptCacheLayout.STABLE
    fallback_layout: PromptCacheLayout = PromptCacheLayout.LEGACY
    previous_release_retained: bool = True
    persistence_migration_required: bool = False

    def validate_for_rollout(self) -> None:
        if self.candidate_layout is self.fallback_layout:
            raise ValueError("candidate and fallback cache layouts must differ")
        if not self.previous_release_retained:
            raise ValueError("the previous cache layout must remain available for one release")
        if self.persistence_migration_required:
            raise ValueError("PCO-07 rollout cannot require persistence migration")

    def selected_layout(self) -> PromptCacheLayout:
        self.validate_for_rollout()
        return self.candidate_layout if self.enabled else self.fallback_layout


class CacheAcceptanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "pco-07.v1"
    candidate_variant: CacheEvaluationVariant = CacheEvaluationVariant.FULL_OPTIMIZATION
    passed: bool
    rollout: CacheRolloutPolicy
    checks: list[CacheGateCheck] = Field(min_length=1)
    blocking_checks: list[str] = Field(default_factory=list)
    provider_reported: bool

    def human_summary(self) -> str:
        state = "PASS" if self.passed else "BLOCKED"
        lines = [
            f"PCO-07 acceptance: {state}",
            f"rollout_layout={self.rollout.selected_layout().value}",
        ]
        lines.extend(
            f"{check.status.value.upper():7} {check.name}: {check.detail}" for check in self.checks
        )
        return "\n".join(lines)


class CacheAcceptanceEvaluator:
    """Evaluate every PCO-07 gate without silently treating missing evidence as pass."""

    def __init__(
        self,
        *,
        baseline_cache_hit_rate: float = 0.362,
        baseline_miss_tokens: int = 141_000,
        miss_threshold_tokens: int = 70_000,
        require_provider_reported: bool = True,
    ) -> None:
        self.baseline_cache_hit_rate = baseline_cache_hit_rate
        self.baseline_miss_tokens = baseline_miss_tokens
        self.miss_threshold_tokens = miss_threshold_tokens
        self.require_provider_reported = require_provider_reported

    def evaluate(
        self,
        report: CacheEvaluationReport,
        *,
        quality: MemoryQualityEvidence | None = None,
        rollout: CacheRolloutPolicy | None = None,
        local_report: CacheEvaluationReport | None = None,
    ) -> CacheAcceptanceReport:
        policy = rollout or CacheRolloutPolicy()
        checks: list[CacheGateCheck] = []
        full_runs = [
            run for run in report.runs if run.variant is CacheEvaluationVariant.FULL_OPTIMIZATION
        ]
        provider_reported = bool(full_runs) and all(
            run.source == "provider_reported" for run in full_runs
        )
        checks.append(
            self._check(
                "provider_reported_cache_usage",
                provider_reported or not self.require_provider_reported,
                provider_reported,
                "three provider-reported runs; deterministic data cannot approve release",
            )
        )
        checks.extend(self._cache_checks(report, full_runs, local_report=local_report))
        checks.extend(self._quality_checks(quality))
        try:
            policy.validate_for_rollout()
            rollout_status = GateStatus.PASS
            rollout_detail = "legacy fallback retained and no persistence migration required"
        except ValueError as exc:
            rollout_status = GateStatus.FAIL
            rollout_detail = str(exc)
        checks.append(
            CacheGateCheck(
                name="gray_rollout_and_rollback",
                status=rollout_status,
                actual=policy.model_dump(mode="json"),
                target="stable candidate, legacy fallback, one-release retention, no migration",
                detail=rollout_detail,
            )
        )
        blocking = [check.name for check in checks if check.status is not GateStatus.PASS]
        return CacheAcceptanceReport(
            passed=not blocking,
            rollout=policy,
            checks=checks,
            blocking_checks=blocking,
            provider_reported=provider_reported,
        )

    def _cache_checks(
        self,
        report: CacheEvaluationReport,
        runs: list[CacheRunReport],
        *,
        local_report: CacheEvaluationReport | None,
    ) -> list[CacheGateCheck]:
        aggregate_hit = _aggregate_hit_rate(runs)
        steady_rates = [
            step.cache_hit_rate
            for run in runs
            for step in run.steps
            if step.cache_hit_rate is not None
            and step.scenario
            not in {
                CacheEvaluationScenario.COLD_START,
                CacheEvaluationScenario.EXPLICIT_COMPRESSION,
                CacheEvaluationScenario.MODEL_SWITCH,
            }
        ]
        steady_median = statistics.median(steady_rates) if steady_rates else None
        max_miss = max(
            (step.cache_miss_tokens or 0 for run in runs for step in run.steps),
            default=None,
        )
        comparison_report = local_report or report
        current_runs = [
            run
            for run in comparison_report.runs
            if run.variant is CacheEvaluationVariant.CURRENT_LAYOUT
        ]
        full_input_runs = [
            run
            for run in comparison_report.runs
            if run.variant is CacheEvaluationVariant.FULL_OPTIMIZATION
        ]
        current_input = _mean([run.input_tokens for run in current_runs])
        full_input = _mean([run.input_tokens for run in full_input_runs])
        reduction = (
            (current_input - full_input) / current_input
            if current_input is not None and current_input > 0 and full_input is not None
            else None
        )
        unexpected = sum(_unexpected_prefix_changes(run) for run in runs)
        large_misses = [
            step
            for run in runs
            for step in run.steps
            if (step.cache_miss_tokens or 0) > self.miss_threshold_tokens
        ]
        attributed = all(
            step.primary_reason
            not in {CacheLayoutReason.UNKNOWN, CacheLayoutReason.PROVIDER_BEST_EFFORT}
            for step in large_misses
        )
        return [
            self._check(
                "three_valid_runs",
                len(runs) >= 3,
                len(runs),
                ">= 3 full-optimization runs",
            ),
            self._check(
                "aggregate_cache_hit_rate",
                aggregate_hit is not None and aggregate_hit >= 0.70,
                aggregate_hit,
                ">= 0.700",
            ),
            self._check(
                "steady_state_cache_hit_median",
                steady_median is not None and steady_median >= 0.85,
                steady_median,
                ">= 0.850 after cold start, epoch rollover, and model switch",
            ),
            self._check(
                "single_request_miss_bound",
                max_miss is not None
                and (
                    max_miss <= self.miss_threshold_tokens
                    or max_miss <= self.baseline_miss_tokens * 0.5
                ),
                max_miss,
                f"<= {self.miss_threshold_tokens} or <= 50% of baseline",
            ),
            self._check(
                "stable_prefix_fingerprints",
                unexpected == 0,
                unexpected,
                "zero unexpected system/project/tool fingerprint changes within an epoch",
            ),
            self._check(
                "large_miss_attribution",
                attributed,
                len(large_misses),
                "every large miss has a structural reason",
            ),
            self._check(
                "compression_prefix_reuse",
                (local_report or report).compression_prefix_reusable,
                (local_report or report).compression_prefix_reusable,
                "local compression request reuses the frozen prefix",
            ),
            self._check(
                "input_token_reduction",
                reduction is not None and reduction >= 0.20,
                reduction,
                ">= 20% versus current layout; cache hits are not token reduction",
            ),
        ]

    def _quality_checks(self, quality: MemoryQualityEvidence | None) -> list[CacheGateCheck]:
        if quality is None:
            return [
                CacheGateCheck(
                    name="memory_quality_and_safety",
                    status=GateStatus.BLOCKED,
                    target="public 1/1, hidden 5/5, safety/staleness 0, no regression",
                    detail="quality evidence is required; no cache result can substitute for it",
                )
            ]
        checks = [
            self._check(
                "public_tests",
                quality.public_passed == 1 and quality.public_total == 1,
                f"{quality.public_passed}/{quality.public_total}",
                "1/1",
            ),
            self._check(
                "hidden_tests",
                quality.hidden_passed == 5 and quality.hidden_total == 5,
                f"{quality.hidden_passed}/{quality.hidden_total}",
                "5/5",
            ),
            self._check(
                "out_of_bounds_changes",
                quality.out_of_bounds_changes == 0,
                quality.out_of_bounds_changes,
                "0",
            ),
            self._check("secret_leaks", quality.secret_leaks == 0, quality.secret_leaks, "0"),
            self._check(
                "stale_fact_hits", quality.stale_fact_hits == 0, quality.stale_fact_hits, "0"
            ),
        ]
        comparisons = (
            (
                "memory_success_rate",
                quality.candidate_success_rate,
                quality.baseline_success_rate,
                True,
            ),
            (
                "critical_fact_recall",
                quality.candidate_critical_fact_recall,
                quality.baseline_critical_fact_recall,
                True,
            ),
            (
                "stale_fact_rate",
                quality.candidate_stale_fact_rate,
                quality.baseline_stale_fact_rate,
                False,
            ),
            (
                "repeated_failure_risk",
                quality.candidate_repeated_failure_risk,
                quality.baseline_repeated_failure_risk,
                False,
            ),
            (
                "recovery_consistency",
                quality.candidate_recovery_consistency,
                quality.baseline_recovery_consistency,
                True,
            ),
        )
        for name, candidate, baseline, higher_is_better in comparisons:
            checks.append(
                self._check(
                    name,
                    candidate is not None
                    and baseline is not None
                    and (candidate >= baseline if higher_is_better else candidate <= baseline),
                    {"candidate": candidate, "baseline": baseline},
                    "candidate must not regress versus baseline",
                )
            )
        return checks

    @staticmethod
    def _check(name: str, passed: bool, actual: Any, target: str) -> CacheGateCheck:
        return CacheGateCheck(
            name=name,
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            actual=actual,
            target=target,
            detail="threshold satisfied" if passed else "threshold not satisfied",
        )


def _aggregate_hit_rate(runs: list[CacheRunReport]) -> float | None:
    hits = sum(run.cache_hit_tokens or 0 for run in runs)
    misses = sum(run.cache_miss_tokens or 0 for run in runs)
    return hits / (hits + misses) if hits + misses else None


def _mean(values: list[int | None]) -> float | None:
    selected = [float(value) for value in values if value is not None]
    return sum(selected) / len(selected) if selected else None


def _unexpected_prefix_changes(run: CacheRunReport) -> int:
    count = 0
    for step in run.steps:
        if step.primary_reason is CacheLayoutReason.DYNAMIC_SYSTEM_PREFIX:
            count += 1
        if step.primary_reason is CacheLayoutReason.TOOL_SCHEMA_CHANGE:
            count += 1
        if (
            step.primary_reason is CacheLayoutReason.PROJECT_SNAPSHOT_CHANGE
            and step.scenario is not CacheEvaluationScenario.PROJECT_CHANGE
        ):
            count += 1
    return count


__all__ = [
    "CacheAcceptanceEvaluator",
    "CacheAcceptanceReport",
    "CacheGateCheck",
    "CacheRolloutPolicy",
    "GateStatus",
    "MemoryQualityEvidence",
]
