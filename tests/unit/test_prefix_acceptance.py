"""PPS gates reject missing evidence, unpaired costs and quality regressions."""

import pytest

from patchloop.domain import PromptCacheLayout
from patchloop.evaluation.cache import (
    CacheBenchmarkRunner,
    CacheEvaluationReport,
    CacheEvaluationScenario,
    CacheEvaluationVariant,
    CacheRunReport,
    CacheSimulationStep,
    append_only_overhead_fixture_fingerprint,
    summarize_cache_run,
)
from patchloop.evaluation.gates import (
    AopEvidenceFile,
    AopOptimizationEvidence,
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


def _aop_evidence_report():
    runs = []
    fixture = append_only_overhead_fixture_fingerprint()
    for version in ("baseline_v1", "balanced_v1"):
        candidate = version == "balanced_v1"
        for case in ("contract-migration", "long-output"):
            for repeat in (1, 2, 3):
                step = CacheSimulationStep(
                    step=0,
                    scenario=CacheEvaluationScenario.WARM_CONTINUATION,
                    request_fingerprint="a" * 64,
                    comparison_kind="ordinary",
                    metric_basis="normalized_messages_v1",
                    previous_request_is_prefix=True,
                    tools_unchanged=True,
                    binding_unchanged=True,
                )
                runs.append(
                    CacheRunReport(
                        variant=CacheEvaluationVariant.APPEND_ONLY,
                        repeat=repeat,
                        source="deterministic",
                        provider="fake",
                        task_case=case,
                        steps=[step],
                        optimization_version=version,
                        projection_format="structured_v1" if candidate else "legacy_v1",
                        new_memory_tokens=80 if candidate else 200,
                        working_item_count=4 if candidate else 1,
                        opaque_working_blob_count=0 if candidate else 1,
                        compression_count=2 if candidate else 4,
                        compression_request_count=2 if candidate else 4,
                        total_estimated_input_tokens=900 if candidate else 1_000,
                        fixed_action_fingerprint=(case[0] + str(repeat))
                        .encode()
                        .hex()
                        .ljust(64, "0"),
                        working_update_count=100,
                        unrelated_working_republication_count=0,
                    )
                )
    report = CacheEvaluationReport(
        schema_version="aop-overhead.v1",
        suite_id="append-only-overhead",
        repeats=3,
        variants=(CacheEvaluationVariant.APPEND_ONLY,),
        scenarios=(),
        fixture_fingerprint=fixture,
        deterministic_fingerprint="b" * 64,
        compression_prefix_reusable=True,
        runs=runs,
        summaries=[summarize_cache_run(runs[0])],
        offline_checks={
            "prefix_invariants": True,
            "recovery": True,
            "single_item_updates": True,
        },
        revision="revision-a",
        source_fingerprint="c" * 64,
    )
    evidence = AopOptimizationEvidence(
        revision="revision-a",
        source_fingerprint="c" * 64,
        fixture_fingerprint=fixture,
        evidence_files=[AopEvidenceFile(path="overhead.json", sha256="d" * 64)],
    )
    return report, evidence


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


def test_missing_fault_matrix_evidence_is_unverified():
    report, local, quality = _evidence()
    quality = quality.model_copy(update={"fault_matrix_passed": None})
    result = _evaluate(report, local, quality)
    check = next(item for item in result.checks if item.name == "offline_recovery")
    assert check.status is GateStatus.UNVERIFIED


def test_aop_gate_accepts_only_complete_offline_overhead_evidence():
    report, evidence = _aop_evidence_report()

    result = CacheAcceptanceEvaluator().evaluate_optimization(report, evidence=evidence)

    assert result.schema_version == "aop.v1"
    assert result.ready_for_bounded_validation
    assert result.optimization_version == "balanced_v1"
    assert next(c for c in result.checks if c.name == "bounded_validation_only").status is (
        GateStatus.PASS
    )


@pytest.mark.parametrize("path", ["../outside.json", "/absolute.json", "C:/report.json"])
def test_aop_evidence_paths_must_be_portable_and_relative(path):
    with pytest.raises(ValueError, match="portable relative path"):
        AopEvidenceFile(path=path, sha256="d" * 64)


@pytest.mark.parametrize(
    ("report_change", "evidence_change", "failed_check"),
    [
        ({"runs": None}, {}, "three_fixed_repeats"),
        ({}, {"fixture_fingerprint": "e" * 64}, "fixture_binding"),
        ({}, {"source_fingerprint": "e" * 64}, "source_binding"),
        ({"suite_id": "pps-prefix-runtime"}, {}, "append_only_overhead_suite"),
    ],
)
def test_aop_gate_rejects_missing_performance_and_provenance_drift(
    report_change, evidence_change, failed_check
):
    report, evidence = _aop_evidence_report()
    if report_change.get("runs") is None and "runs" in report_change:
        report_change = {"runs": report.runs[:-1]}
    report = report.model_copy(update=report_change)
    evidence = evidence.model_copy(update=evidence_change)

    result = CacheAcceptanceEvaluator().evaluate_optimization(report, evidence=evidence)

    assert not result.ready_for_bounded_validation
    assert next(c for c in result.checks if c.name == failed_check).status is GateStatus.FAIL


def test_aop_fixed_actions_cannot_masquerade_as_provider_reported_usage():
    report, evidence = _aop_evidence_report()
    report = report.model_copy(
        update={
            "runs": [
                report.runs[0].model_copy(update={"source": "provider_reported"}),
                *report.runs[1:],
            ]
        }
    )

    result = CacheAcceptanceEvaluator().evaluate_optimization(report, evidence=evidence)

    assert not result.ready_for_bounded_validation
    check = next(c for c in result.checks if c.name == "offline_evidence_identity")
    assert check.status is GateStatus.FAIL


def test_pps_zero_price_cost_reduction_is_unverified():
    report, local, quality = _evidence()
    report = report.model_copy(
        update={
            "runs": [
                run.model_copy(
                    update={
                        "cost_usd": 0,
                        "steps": [step.model_copy(update={"cost_usd": 0}) for step in run.steps],
                    }
                )
                for run in report.runs
            ]
        }
    )

    result = _evaluate(report, local, quality)

    check = next(c for c in result.checks if c.name == "total_cost_reduction")
    assert check.status is GateStatus.UNVERIFIED


def test_append_only_overhead_fixture_uses_structured_memory_and_fixed_actions(tmp_path):
    baseline = CacheBenchmarkRunner().run_runtime_fixture(
        tmp_path,
        layout=PromptCacheLayout.APPEND_ONLY,
        prefix_suite=True,
        optimization_version="baseline_v1",
        overhead_case="long-output",
    )
    candidate = CacheBenchmarkRunner().run_runtime_fixture(
        tmp_path,
        layout=PromptCacheLayout.APPEND_ONLY,
        prefix_suite=True,
        optimization_version="balanced_v1",
        overhead_case="long-output",
    )

    assert baseline.fixed_action_fingerprint == candidate.fixed_action_fingerprint
    assert baseline.projection_format == "legacy_v1"
    assert baseline.opaque_working_blob_count == 1
    assert candidate.projection_format == "structured_v1"
    assert candidate.opaque_working_blob_count == 0
    assert candidate.working_item_count == 4
    assert candidate.working_update_count == 100
    assert candidate.unrelated_working_republication_count == 0
    assert candidate.compression_request_count <= baseline.compression_request_count
    assert candidate.total_estimated_input_tokens <= baseline.total_estimated_input_tokens


@pytest.mark.parametrize("case", ["ordinary", "compression", "restore"])
def test_prefix_fixture_uses_runtime_without_a_cache_simulator(tmp_path, monkeypatch, case):
    from patchloop.evaluation.cache import CacheBenchmarkRunner

    def reject_simulator(*args, **kwargs):
        raise AssertionError("PPS runtime suite must not use simulated cache usage")

    monkeypatch.setattr(
        "patchloop.evaluation.cache.DeterministicPrefixCacheSimulator", reject_simulator
    )
    run = CacheBenchmarkRunner().run_runtime_fixture(
        tmp_path,
        layout=PromptCacheLayout.APPEND_ONLY,
        prefix_suite=True,
        compress=case == "compression",
        restore=case == "restore",
    )
    assert run.source == "deterministic"
    assert run.restored_request_count == (1 if case == "restore" else 0)
    assert run.input_tokens is run.cache_hit_tokens is run.cost_usd is None
    assert run.ordinary_budget_respected
    assert all(
        s.previous_request_is_prefix is True and s.tools_unchanged and s.binding_unchanged
        for s in run.steps
        if s.comparison_kind in {"ordinary", "compression"}
    )
    if case == "compression":
        assert run.compression_count >= 10
        assert run.max_summary_messages == 1
        assert all(s.source_request_id for s in run.steps if s.comparison_kind == "compression")
    elif case == "restore":
        assert run.recovery_verified
        assert any(s.restored and s.previous_request_is_prefix for s in run.steps)
