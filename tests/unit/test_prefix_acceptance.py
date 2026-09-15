"""PPS gates reject missing evidence, unpaired costs and quality regressions."""

import pytest

from patchloop.domain import PromptCacheLayout
from patchloop.evaluation.cache import (
    CacheEvaluationReport,
    CacheEvaluationScenario,
    CacheEvaluationVariant,
    CacheRunReport,
    CacheSimulationStep,
    summarize_cache_run,
)
from patchloop.evaluation.gates import (
    CacheAcceptanceEvaluator,
    CacheRolloutPolicy,
    GateStatus,
    MemoryQualityEvidence,
)


def _run(variant, case, repeat):
    candidate = variant is CacheEvaluationVariant.APPEND_ONLY
    steps = []
    for index, kind in enumerate(
        ("cold_start", "ordinary", "compression", "epoch_boundary", "ordinary")
    ):
        hit = (90 if kind in {"ordinary", "compression"} else 0) if candidate else 10
        steps.append(
            CacheSimulationStep(
                step=index,
                scenario=CacheEvaluationScenario.WARM_CONTINUATION,
                request_id=f"{variant.value}-{case}-{repeat}-{index}",
                source_request_id=f"{variant.value}-{case}-{repeat}-1"
                if kind == "compression"
                else None,
                request_fingerprint="a" * 64,
                comparison_kind=kind,
                metric_basis="normalized_messages_v1",
                previous_request_is_prefix=kind in {"ordinary", "compression"},
                tools_unchanged=True,
                binding_unchanged=True,
                input_tokens=100,
                output_tokens=10,
                cache_hit_tokens=hit,
                cache_miss_tokens=100 - hit,
                usage_complete=True,
                cost_usd=0.14 if candidate else 0.2,
                cost_status="estimated",
                restored=index == 4,
            )
        )
    return CacheRunReport(
        variant=variant,
        task_case=case,
        repeat=repeat,
        steps=steps,
        task_correctness=1,
        source="provider_reported",
        provider="test-provider",
        request_linkage_complete=True,
        prefix_schema_version="pps.v1",
        model="fixed-model",
        endpoint_fingerprint="b" * 64,
        binding_fingerprint="c" * 64,
        input_budget=8000,
        pricing_version="prices-v1",
        started_at=f"2026-09-15T12:{repeat:02d}:{1 if candidate else 0:02d}+00:00",
        batch_id="same-batch",
        pair_id=f"{case}-{repeat}",
        unknown_usage_attempts=0,
        input_tokens=500,
        cache_hit_tokens=sum(s.cache_hit_tokens for s in steps),
        cache_miss_tokens=sum(s.cache_miss_tokens for s in steps),
        cost_usd=0.7 if candidate else 1,
    )


def _report(runs):
    return CacheEvaluationReport(
        schema_version="pps.v1",
        repeats=3,
        variants=tuple(dict.fromkeys(r.variant for r in runs)),
        fixture_fingerprint="d" * 64,
        runs=runs,
        summaries=[summarize_cache_run(r) for r in runs],
    )


def _evidence():
    real = _report(
        [
            _run(v, case, repeat)
            for v in (CacheEvaluationVariant.CURRENT_LAYOUT, CacheEvaluationVariant.APPEND_ONLY)
            for case in ("contract-migration", "long-output")
            for repeat in (1, 2, 3)
        ]
    )
    local = _report(
        [
            _run(CacheEvaluationVariant.APPEND_ONLY, case, 1).model_copy(
                update={
                    "source": "deterministic",
                    "tool_rounds": 6,
                    "retrieval_state_count": 3,
                    "compression_count": 10,
                    "max_summary_messages": 1,
                    "ordinary_budget_respected": True,
                    "recovery_verified": True,
                }
            )
            for case in ("ordinary", "compression", "restore")
        ]
    )
    quality = MemoryQualityEvidence(
        public_passed=6,
        public_total=6,
        hidden_passed=12,
        hidden_total=12,
        out_of_bounds_changes=0,
        secret_leaks=0,
        stale_fact_hits=0,
        approval_bypasses=0,
        candidate_success_rate=1,
        baseline_success_rate=1,
        candidate_critical_fact_recall=1,
        baseline_critical_fact_recall=1,
        candidate_constraint_recall=1,
        baseline_constraint_recall=1,
        fault_matrix_passed=True,
        verified_task_cases=["contract-migration", "long-output"],
    )
    return real, local, quality


def _evaluate(report, local, quality):
    return CacheAcceptanceEvaluator(require_provider_reported=False).evaluate(
        report,
        profile="pps",
        local_report=local,
        quality=quality,
        rollout=CacheRolloutPolicy(enabled=True, candidate_layout=PromptCacheLayout.APPEND_ONLY),
    )


def test_pps_accepts_complete_paired_evidence_without_fixed_pco_baseline():
    report, local, quality = _evidence()
    result = _evaluate(report, local, quality)
    assert result.passed, result.human_summary()
    assert result.rollout.selected_layout() is PromptCacheLayout.APPEND_ONLY


@pytest.mark.parametrize(
    "change,gate,status",
    [
        ({"source": "deterministic"}, "provider_reported_cache_usage", GateStatus.UNVERIFIED),
        ({"unknown_usage_attempts": 1}, "total_cost_reduction", GateStatus.UNVERIFIED),
        ({"cost_usd": None}, "total_cost_reduction", GateStatus.UNVERIFIED),
        ({"batch_id": "other"}, "paired_batch", GateStatus.FAIL),
        ({"cost_usd": 2.0}, "total_cost_reduction", GateStatus.FAIL),
        ({"pricing_version": None}, "paired_batch", GateStatus.UNVERIFIED),
    ],
)
def test_pps_rejects_simulation_unknown_cost_and_incomparable_runs(change, gate, status):
    report, local, quality = _evidence()
    report = report.model_copy(
        update={
            "runs": [
                r.model_copy(update=change)
                if r.variant is CacheEvaluationVariant.APPEND_ONLY
                else r
                for r in report.runs
            ]
        }
    )
    result = _evaluate(report, local, quality)
    assert not result.passed
    assert next(c for c in result.checks if c.name == gate).status is status
    assert result.rollout.selected_layout() is PromptCacheLayout.LEGACY


def test_missing_cache_fields_and_quality_regression_cannot_pass():
    report, local, quality = _evidence()
    incomplete = report.runs[-1].model_copy(
        update={
            "steps": [
                s.model_copy(update={"cache_hit_tokens": None}) for s in report.runs[-1].steps
            ]
        }
    )
    result = _evaluate(
        report.model_copy(update={"runs": [*report.runs[:-1], incomplete]}), local, quality
    )
    assert next(c for c in result.checks if c.name == "provider_reported_cache_usage").status is (
        GateStatus.UNVERIFIED
    )
    regressed = quality.model_copy(update={"candidate_success_rate": 0.8, "secret_leaks": 1})
    result = _evaluate(report, local, regressed)
    assert not result.passed
    assert {"success", "quality_and_safety"} <= set(result.blocking_checks)


def test_warm_rate_is_weighted_by_tokens():
    report, local, quality = _evidence()
    runs = []
    for run in report.runs:
        steps = []
        for step in run.steps:
            if run.variant is CacheEvaluationVariant.APPEND_ONLY and step.step == 1:
                step = step.model_copy(
                    update={
                        "input_tokens": 10000,
                        "cache_hit_tokens": 6500,
                        "cache_miss_tokens": 3500,
                    }
                )
            steps.append(step)
        runs.append(run.model_copy(update={"steps": steps}))
    result = _evaluate(report.model_copy(update={"runs": runs}), local, quality)
    warm = next(c for c in result.checks if c.name == "warm_cache_hit_rate")
    assert warm.status is GateStatus.FAIL
    assert warm.actual == pytest.approx(6590 / 10100)


@pytest.mark.parametrize("case", ["ordinary", "compression", "restore"])
def test_prefix_fixture_uses_runtime_without_a_cache_simulator(tmp_path, monkeypatch, case):
    from patchloop.evaluation.cache import CacheBenchmarkRunner

    def reject_simulator(*args, **kwargs):
        raise AssertionError("PPS runtime suite must not use simulated cache usage")

    monkeypatch.setattr(
        "patchloop.evaluation.cache.DeterministicPrefixCacheSimulator", reject_simulator
    )
    run = CacheBenchmarkRunner().run_runtime_fixture(
        tmp_path, layout=PromptCacheLayout.APPEND_ONLY, prefix_suite=True,
        compress=case == "compression", restore=case == "restore",
    )
    assert run.source == "deterministic"
    assert run.restored_request_count == (1 if case == "restore" else 0)
    assert run.input_tokens is run.cache_hit_tokens is run.cost_usd is None
    assert run.ordinary_budget_respected
    assert all(s.previous_request_is_prefix is True and s.tools_unchanged and s.binding_unchanged
               for s in run.steps if s.comparison_kind in {"ordinary", "compression"})
    if case == "compression":
        assert run.compression_count >= 10
        assert run.max_summary_messages == 1
        assert all(s.source_request_id for s in run.steps if s.comparison_kind == "compression")
    elif case == "restore":
        assert run.recovery_verified
        assert any(s.restored and s.previous_request_is_prefix for s in run.steps)
