"""PCO-07 acceptance gates, gray rollout policy, and recovery evidence."""

from __future__ import annotations

import statistics
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.domain import PromptCacheLayout
from patchloop.evaluation.cache import (
    CacheEvaluationReport,
    CacheEvaluationScenario,
    CacheEvaluationVariant,
    CacheRunReport,
)
from patchloop.prompt_cache import CacheLayoutReason


class GateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    UNVERIFIED = "unverified"


class CacheGateCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    status: GateStatus
    actual: Any = None
    target: str
    detail: str


class AopEvidenceFile(BaseModel):
    """One content-addressed input relative to the readiness report directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_relative_path(self) -> AopEvidenceFile:
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts or ":" in self.path or "\\" in self.path:
            raise ValueError("AOP evidence path must be a portable relative path")
        return self


class AopOptimizationEvidence(BaseModel):
    """Provenance supplied by the CLI for an offline optimization report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: str = Field(min_length=1)
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    optimization_version: Literal["balanced_v1"] = "balanced_v1"
    baseline_version: Literal["baseline_v1"] = "baseline_v1"
    evidence_files: list[AopEvidenceFile] = Field(min_length=1)


class AopReadinessReport(BaseModel):
    """L0 result. Passing authorizes only a bounded real validation run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["aop.v1"] = "aop.v1"
    revision: str = Field(min_length=1)
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    optimization_version: Literal["balanced_v1"] = "balanced_v1"
    baseline_version: Literal["baseline_v1"] = "baseline_v1"
    checks: list[CacheGateCheck] = Field(min_length=1)
    evidence_files: list[AopEvidenceFile] = Field(min_length=1)
    ready_for_bounded_validation: bool

    def human_summary(self) -> str:
        state = "READY" if self.ready_for_bounded_validation else "BLOCKED"
        lines = [f"AOP L0 readiness: {state}"]
        lines.extend(
            f"{check.status.value.upper():10} {check.name}: {check.detail}" for check in self.checks
        )
        return "\n".join(lines)


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
    verified_task_cases: list[str] = Field(default_factory=list)
    candidate_constraint_recall: float | None = Field(default=None, ge=0.0, le=1.0)
    baseline_constraint_recall: float | None = Field(default=None, ge=0.0, le=1.0)
    approval_bypasses: int | None = Field(default=None, ge=0)
    fault_matrix_passed: bool | None = None


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
            f"{'PPS' if self.schema_version.startswith('pps') else 'PCO-07'} acceptance: {state}",
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
        profile: str = "pco",
        baseline_report: CacheEvaluationReport | None = None,
    ) -> CacheAcceptanceReport:
        if profile == "pps":
            return self._evaluate_prefix(
                report,
                baseline_report=baseline_report,
                local_report=local_report,
                quality=quality,
                rollout=rollout,
            )
        if profile != "pco":
            raise ValueError("cache gate profile must be pco or pps")
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

    def evaluate_optimization(
        self,
        report: CacheEvaluationReport,
        *,
        evidence: AopOptimizationEvidence,
    ) -> AopReadinessReport:
        """Evaluate AOP L0 structure and overhead without approving real benefits."""

        checks: list[CacheGateCheck] = []

        def check(name: str, passed: bool, actual: Any, target: str) -> None:
            checks.append(self._check(name, passed, actual, target))

        check(
            "append_only_overhead_suite",
            report.schema_version == "aop-overhead.v1"
            and report.suite_id == "append-only-overhead",
            {"schema_version": report.schema_version, "suite_id": report.suite_id},
            "dedicated append-only-overhead report; stress/PPS fixtures are not interchangeable",
        )
        check(
            "fixture_binding",
            report.fixture_fingerprint == evidence.fixture_fingerprint,
            {
                "report": report.fixture_fingerprint,
                "evidence": evidence.fixture_fingerprint,
            },
            "report fixture fingerprint equals the readiness evidence fingerprint",
        )
        check(
            "source_binding",
            report.revision == evidence.revision
            and report.source_fingerprint == evidence.source_fingerprint,
            {
                "report_revision": report.revision,
                "current_revision": evidence.revision,
                "report_source": report.source_fingerprint,
                "current_source": evidence.source_fingerprint,
            },
            "report revision and source fingerprint match the current checkout",
        )
        runs = report.runs
        baseline = [run for run in runs if run.optimization_version == evidence.baseline_version]
        candidate = [
            run for run in runs if run.optimization_version == evidence.optimization_version
        ]
        cases = {"contract-migration", "long-output"}
        expected_repeats = {1, 2, 3}
        repeat_shape = (
            report.repeats == 3
            and len(runs) == 12
            and all(
                {run.repeat for run in selected if run.task_case == case} == expected_repeats
                and len([run for run in selected if run.task_case == case]) == 3
                for selected in (baseline, candidate)
                for case in cases
            )
            and {run.task_case for run in runs} == cases
        )
        check(
            "three_fixed_repeats",
            repeat_shape,
            {
                "baseline": len(baseline),
                "candidate": len(candidate),
                "declared_repeats": report.repeats,
            },
            "baseline_v1 and balanced_v1 each run both scenarios exactly three times",
        )
        deterministic = bool(runs) and all(
            run.source == "deterministic"
            and run.provider == "fake"
            and run.input_tokens is None
            and run.cache_hit_tokens is None
            and run.cache_miss_tokens is None
            and run.cost_usd is None
            and all(
                step.cache_hit_tokens is None
                and step.cache_miss_tokens is None
                and step.cost_usd is None
                for step in run.steps
            )
            for run in runs
        )
        check(
            "offline_evidence_identity",
            deterministic,
            sorted({(run.source, run.provider) for run in runs}),
            "fixed-action data remains deterministic/fake and never claims provider-reported usage",
        )
        paired_actions = bool(baseline and candidate)
        for case in cases:
            for repeat in expected_repeats:
                pair = [run for run in runs if run.task_case == case and run.repeat == repeat]
                paired_actions = (
                    paired_actions
                    and len(pair) == 2
                    and pair[0].fixed_action_fingerprint is not None
                    and pair[0].fixed_action_fingerprint == pair[1].fixed_action_fingerprint
                )
        check(
            "fixed_actions",
            paired_actions,
            len({run.fixed_action_fingerprint for run in runs}),
            "each baseline/candidate pair uses the same fixed action fingerprint",
        )
        structured = bool(candidate) and all(
            run.projection_format == "structured_v1"
            and run.opaque_working_blob_count == 0
            and (run.working_item_count or 0) > 0
            for run in candidate
        )
        check(
            "structured_working_memory",
            structured,
            {
                "formats": sorted(str(run.projection_format) for run in candidate),
                "opaque_blobs": sum(run.opaque_working_blob_count or 0 for run in candidate),
            },
            "balanced_v1 uses structured_v1 with zero opaque working blobs",
        )
        update_stream = (
            report.offline_checks.get("single_item_updates") is True
            and bool(candidate)
            and all(
                run.working_update_count == 100 and run.unrelated_working_republication_count == 0
                for run in candidate
            )
        )
        check(
            "single_item_update_stream",
            update_stream,
            {
                "updates": [run.working_update_count for run in candidate],
                "unrelated": [run.unrelated_working_republication_count for run in candidate],
            },
            "100 updates publish no unrelated working entries, including read_file insertion",
        )
        prefix_recovery = (
            report.offline_checks.get("prefix_invariants") is True
            and report.offline_checks.get("recovery") is True
        )
        check(
            "prefix_and_recovery",
            prefix_recovery,
            report.offline_checks,
            "ordinary/compression prefix invariants and checkpoint recovery pass",
        )

        baseline_memory = sum(run.new_memory_tokens or 0 for run in baseline)
        candidate_memory = sum(run.new_memory_tokens or 0 for run in candidate)
        memory_complete = all(
            run.compression_request_count == 0 or run.new_memory_tokens is not None for run in runs
        )
        memory_reduction = (
            (baseline_memory - candidate_memory) / baseline_memory
            if memory_complete and baseline_memory > 0
            else None
        )
        check(
            "new_memory_token_reduction",
            memory_reduction is not None and memory_reduction >= 0.50,
            {
                "baseline": baseline_memory,
                "candidate": candidate_memory,
                "reduction": memory_reduction,
            },
            "combined estimated new-memory tokens decrease by at least 50%",
        )
        baseline_compressions = sum(run.compression_request_count or 0 for run in baseline)
        candidate_compressions = sum(run.compression_request_count or 0 for run in candidate)
        per_case_nonincrease = all(
            sum(run.compression_request_count or 0 for run in candidate if run.task_case == case)
            <= sum(run.compression_request_count or 0 for run in baseline if run.task_case == case)
            for case in cases
        )
        compression_reduction = (
            (baseline_compressions - candidate_compressions) / baseline_compressions
            if baseline_compressions > 0
            else None
        )
        check(
            "compression_call_reduction",
            per_case_nonincrease
            and compression_reduction is not None
            and compression_reduction >= 0.25,
            {
                "baseline": baseline_compressions,
                "candidate": candidate_compressions,
                "reduction": compression_reduction,
            },
            "no scenario increases compressions and combined calls decrease by at least 25%",
        )
        input_complete = all(run.total_estimated_input_tokens is not None for run in runs)
        baseline_input = sum(run.total_estimated_input_tokens or 0 for run in baseline)
        candidate_input = sum(run.total_estimated_input_tokens or 0 for run in candidate)
        check(
            "estimated_input_nonincrease",
            input_complete and candidate_input <= baseline_input,
            {"baseline": baseline_input, "candidate": candidate_input},
            "combined total estimated input does not increase",
        )
        check(
            "bounded_validation_only",
            True,
            "l0-offline",
            "readiness authorizes bounded validation only, not PPS benefit or rollout",
        )
        ready = all(item.status is GateStatus.PASS for item in checks)
        return AopReadinessReport(
            revision=evidence.revision,
            source_fingerprint=evidence.source_fingerprint,
            fixture_fingerprint=evidence.fixture_fingerprint,
            optimization_version=evidence.optimization_version,
            baseline_version=evidence.baseline_version,
            checks=checks,
            evidence_files=evidence.evidence_files,
            ready_for_bounded_validation=ready,
        )

    def _evaluate_prefix(
        self,
        report: CacheEvaluationReport,
        *,
        baseline_report: CacheEvaluationReport | None,
        local_report: CacheEvaluationReport | None,
        quality: MemoryQualityEvidence | None,
        rollout: CacheRolloutPolicy | None,
    ) -> CacheAcceptanceReport:
        candidates = [r for r in report.runs if r.variant is CacheEvaluationVariant.APPEND_ONLY]
        baselines = [
            r
            for r in (baseline_report or report).runs
            if r.variant is CacheEvaluationVariant.CURRENT_LAYOUT
        ]
        local = [
            r
            for r in (local_report or report).runs
            if r.variant is CacheEvaluationVariant.APPEND_ONLY
        ]
        checks: list[CacheGateCheck] = []

        def check(name: str, value: bool | None, actual: Any, target: str) -> None:
            checks.append(
                CacheGateCheck(
                    name=name,
                    status=GateStatus.UNVERIFIED
                    if value is None
                    else GateStatus.PASS
                    if value
                    else GateStatus.FAIL,
                    actual=actual,
                    target=target,
                    detail="evidence unavailable"
                    if value is None
                    else "requirement satisfied"
                    if value
                    else "requirement not satisfied",
                )
            )

        steps = [s for r in candidates for s in r.steps]
        ordinary = [s for s in steps if s.comparison_kind == "ordinary"]
        compression = [s for s in steps if s.comparison_kind == "compression"]
        complete_metrics = bool(steps) and all(
            s.metric_basis == "normalized_messages_v1" for s in steps
        )
        check(
            "ordinary_prefix",
            all(
                s.previous_request_is_prefix is True
                and s.tools_unchanged is True
                and s.binding_unchanged is True
                for s in ordinary
            )
            if complete_metrics and ordinary
            else None,
            {"samples": len(ordinary), "missing": sum(s.metric_basis is None for s in steps)},
            "100% ordinary message prefixes, tools and binding; zero missing samples",
        )
        sources_valid = True
        for run in candidates:
            prior_request_id = None
            for item in run.steps:
                if item.comparison_kind == "compression":
                    sources_valid = (
                        sources_valid
                        and prior_request_id is not None
                        and (item.source_request_id == prior_request_id)
                    )
                else:
                    prior_request_id = item.request_id
        check(
            "compression_source",
            sources_valid
            and all(
                s.previous_request_is_prefix is True
                and s.tools_unchanged is True
                and s.binding_unchanged is True
                and s.source_request_id is not None
                and any(
                    p.request_id == s.source_request_id and p.comparison_kind != "compression"
                    for p in steps
                )
                for s in compression
            )
            if complete_metrics and compression
            else None,
            len(compression),
            "every compression reuses its complete declared ordinary source",
        )
        local_ordinary = [r for r in local if r.task_case == "ordinary"]
        check(
            "runtime_fixture_coverage",
            all(
                (r.tool_rounds or 0) >= 6 and (r.retrieval_state_count or 0) >= 3
                for r in local_ordinary
            )
            if local_ordinary
            else None,
            len(local_ordinary),
            "6 tool rounds and 3 retrieval states",
        )
        long_runs = [r for r in local if r.task_case == "compression"]
        check(
            "bounded_epochs",
            all(
                r.compression_count >= 10
                and r.max_summary_messages == 1
                and r.ordinary_budget_respected is True
                for r in long_runs
            )
            if long_runs
            else None,
            [r.compression_count for r in long_runs],
            "10 compressions, one current summary and all ordinary inputs within budget",
        )
        restored = [r for r in local if r.task_case == "restore"]
        check(
            "offline_recovery",
            all(r.recovery_verified is True for r in restored)
            and quality.fault_matrix_passed is True
            if restored and quality is not None and quality.fault_matrix_passed is not None
            else None,
            len(restored),
            "Runtime checkpoint restore and full fault matrix passed",
        )

        real = bool(candidates and baselines) and all(
            r.source == "provider_reported" and r.provider != "fake"
            for r in [*candidates, *baselines]
        )
        usage_complete = real and all(
            r.request_linkage_complete
            and r.unknown_usage_attempts == 0
            and all(
                s.usage_complete is True
                and s.input_tokens is not None
                and s.output_tokens is not None
                and s.cache_hit_tokens is not None
                and s.cache_miss_tokens is not None
                and s.cache_hit_tokens + s.cache_miss_tokens == s.input_tokens
                for s in r.steps
            )
            for r in [*candidates, *baselines]
        )
        check(
            "provider_reported_cache_usage",
            True if usage_complete else None,
            real,
            "complete request-associated Provider usage; simulations cannot pass",
        )
        pairs = {(r.task_case, r.repeat, r.pair_id): r for r in baselines}
        cases = {r.task_case for r in candidates if r.task_case is not None}
        metadata_keys = (
            "model",
            "endpoint_fingerprint",
            "binding_fingerprint",
            "input_budget",
            "pricing_version",
            "batch_id",
            "started_at",
            "pair_id",
        )
        metadata_complete = real and all(
            all(getattr(r, key) is not None for key in metadata_keys)
            for r in [*candidates, *baselines]
        )
        comparable = len(cases) >= 2 and len(pairs) == len(baselines) == len(candidates)
        for candidate in candidates:
            baseline = pairs.get((candidate.task_case, candidate.repeat, candidate.pair_id))
            comparable = (
                comparable
                and baseline is not None
                and all(
                    getattr(candidate, key) == getattr(baseline, key)
                    for key in metadata_keys
                    if key != "started_at"
                )
            )
        comparable = comparable and len({r.batch_id for r in [*candidates, *baselines]}) == 1
        comparable = comparable and all(
            len([r for r in candidates if r.task_case == case]) == 3
            and {r.repeat for r in candidates if r.task_case == case} == {1, 2, 3}
            for case in cases
        )
        if metadata_complete and comparable:
            try:
                for case in cases:
                    ordered = sorted(
                        [r for r in [*candidates, *baselines] if r.task_case == case],
                        key=lambda r: datetime.fromisoformat(r.started_at or ""),
                    )
                    comparable = comparable and all(
                        ordered[i].pair_id == ordered[i + 1].pair_id
                        and ordered[i].variant is not ordered[i + 1].variant
                        for i in range(0, len(ordered), 2)
                    )
            except (ValueError, TypeError):
                comparable = False
        check(
            "paired_batch",
            comparable if metadata_complete else None,
            {
                "candidate_runs": len(candidates),
                "baseline_runs": len(baselines),
                "cases": sorted(cases),
            },
            "two task cases, three interleaved pairs each; "
            "same batch/model/endpoint/budget/pricing",
        )

        def prefix_rate(runs: list[CacheRunReport]) -> float | None:
            hits = sum(s.cache_hit_tokens or 0 for r in runs for s in r.steps)
            misses = sum(s.cache_miss_tokens or 0 for r in runs for s in r.steps)
            return hits / (hits + misses) if usage_complete and hits + misses > 0 else None

        hit = prefix_rate(candidates)
        base_hit = prefix_rate(baselines)
        warm = [s for r in candidates for s in r.steps if s.comparison_kind == "ordinary"]
        warm_hit = (
            (
                sum(s.cache_hit_tokens or 0 for s in warm)
                / sum((s.cache_hit_tokens or 0) + (s.cache_miss_tokens or 0) for s in warm)
            )
            if (
                usage_complete
                and warm
                and all(s.usage_complete is True for s in warm)
                and sum((s.cache_hit_tokens or 0) + (s.cache_miss_tokens or 0) for s in warm) > 0
            )
            else None
        )
        check(
            "warm_cache_hit_rate",
            warm_hit >= 0.70 if warm_hit is not None else None,
            warm_hit,
            "weighted warm hit rate >= 70%, excluding epoch first requests",
        )
        check(
            "cache_hit_rate_gain",
            hit - base_hit >= 0.20
            if hit is not None and base_hit is not None and metadata_complete and comparable
            else None,
            {"candidate": hit, "baseline": base_hit},
            "all-request hit rate gain >= 20 percentage points",
        )
        cost_complete = (
            usage_complete
            and metadata_complete
            and comparable
            and all(
                r.cost_usd is not None
                and r.unknown_usage_attempts == 0
                and r.request_linkage_complete
                and all(s.cost_status == "estimated" for s in r.steps)
                for r in [*candidates, *baselines]
            )
        )
        cost_by_case = (
            {
                case: {
                    "candidate": statistics.median(
                        r.cost_usd
                        for r in candidates
                        if r.task_case == case and r.cost_usd is not None
                    ),
                    "baseline": statistics.median(
                        r.cost_usd
                        for r in baselines
                        if r.task_case == case and r.cost_usd is not None
                    ),
                }
                for case in cases
            }
            if cost_complete
            else {}
        )
        cost_verifiable = (
            cost_complete
            and bool(cost_by_case)
            and all(values["baseline"] > 0 for values in cost_by_case.values())
        )
        check(
            "total_cost_reduction",
            all(v["candidate"] <= v["baseline"] * 0.8 for v in cost_by_case.values())
            if cost_verifiable
            else None,
            cost_by_case,
            "each task's median total known cost decreases >= 20%, including compression/attempts",
        )
        check(
            "real_resume",
            any(
                s.restored
                and s.comparison_kind == "ordinary"
                and s.previous_request_is_prefix is True
                for s in steps
            )
            if real
            else None,
            sum(s.restored for s in steps),
            "at least one real pause/resume with a verified prefix",
        )
        if quality is None:
            check(
                "quality_and_safety",
                None,
                None,
                "complete quality and safety evidence for both tasks",
            )
        else:
            required = (
                quality.public_passed,
                quality.public_total,
                quality.hidden_passed,
                quality.hidden_total,
                quality.out_of_bounds_changes,
                quality.secret_leaks,
                quality.stale_fact_hits,
                quality.approval_bypasses,
            )
            passed = (
                quality.public_passed == quality.public_total
                and (quality.hidden_passed == quality.hidden_total)
                and all(
                    value == 0
                    for value in (
                        quality.out_of_bounds_changes,
                        quality.secret_leaks,
                        quality.stale_fact_hits,
                        quality.approval_bypasses,
                    )
                )
            )
            check(
                "quality_and_safety",
                passed and cases <= set(quality.verified_task_cases)
                if all(v is not None for v in required) and len(cases) >= 2
                else None,
                quality.model_dump(mode="json"),
                "all public/hidden checks pass; unsafe/stale uses zero",
            )
            for name, candidate_value, baseline_value in (
                ("success", quality.candidate_success_rate, quality.baseline_success_rate),
                (
                    "critical_fact_recall",
                    quality.candidate_critical_fact_recall,
                    quality.baseline_critical_fact_recall,
                ),
                (
                    "constraint_recall",
                    quality.candidate_constraint_recall,
                    quality.baseline_constraint_recall,
                ),
            ):
                check(
                    name,
                    candidate_value >= baseline_value
                    if candidate_value is not None and baseline_value is not None
                    else None,
                    {"candidate": candidate_value, "baseline": baseline_value},
                    "no regression versus paired baseline",
                )
        policy = rollout or CacheRolloutPolicy(candidate_layout=PromptCacheLayout.APPEND_ONLY)
        policy.validate_for_rollout()
        check(
            "rollout_candidate",
            policy.candidate_layout is PromptCacheLayout.APPEND_ONLY
            and policy.fallback_layout is PromptCacheLayout.LEGACY,
            policy.candidate_layout.value,
            "append_only candidate with legacy fallback",
        )
        blocking = [c.name for c in checks if c.status is not GateStatus.PASS]
        return CacheAcceptanceReport(
            schema_version="pps.v1",
            candidate_variant=CacheEvaluationVariant.APPEND_ONLY,
            passed=not blocking,
            checks=checks,
            blocking_checks=blocking,
            provider_reported=real,
            rollout=policy.model_copy(update={"enabled": policy.enabled and not blocking}),
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
    "AopEvidenceFile",
    "AopOptimizationEvidence",
    "AopReadinessReport",
    "CacheAcceptanceEvaluator",
    "CacheAcceptanceReport",
    "CacheGateCheck",
    "CacheRolloutPolicy",
    "GateStatus",
    "MemoryQualityEvidence",
]
