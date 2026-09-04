from patchloop.evaluation import (
    CacheAcceptanceEvaluator,
    CacheBenchmarkRunner,
    CacheRolloutPolicy,
    GateStatus,
    MemoryQualityEvidence,
)


def _quality() -> MemoryQualityEvidence:
    return MemoryQualityEvidence(
        public_passed=1,
        public_total=1,
        hidden_passed=5,
        hidden_total=5,
        out_of_bounds_changes=0,
        secret_leaks=0,
        stale_fact_hits=0,
        candidate_success_rate=1.0,
        baseline_success_rate=1.0,
        candidate_critical_fact_recall=1.0,
        baseline_critical_fact_recall=1.0,
        candidate_stale_fact_rate=0.0,
        baseline_stale_fact_rate=0.0,
        candidate_repeated_failure_risk=0.0,
        baseline_repeated_failure_risk=0.0,
        candidate_recovery_consistency=1.0,
        baseline_recovery_consistency=1.0,
    )


def test_pco07_local_dry_run_checks_all_cache_and_quality_gates() -> None:
    report = CacheBenchmarkRunner(repeats=3).run()
    acceptance = CacheAcceptanceEvaluator(require_provider_reported=False).evaluate(
        report,
        local_report=report,
        quality=_quality(),
        rollout=CacheRolloutPolicy(enabled=True),
    )

    assert acceptance.passed
    assert acceptance.blocking_checks == []
    assert all(check.status is GateStatus.PASS for check in acceptance.checks)


def test_pco07_does_not_approve_without_provider_or_memory_evidence() -> None:
    report = CacheBenchmarkRunner(repeats=3).run()
    acceptance = CacheAcceptanceEvaluator().evaluate(report)

    assert acceptance.passed is False
    assert "provider_reported_cache_usage" in acceptance.blocking_checks
    assert "memory_quality_and_safety" in acceptance.blocking_checks


def test_rollout_policy_keeps_legacy_fallback_without_migration() -> None:
    policy = CacheRolloutPolicy(enabled=True)
    assert policy.selected_layout().value == "stable"
    assert CacheRolloutPolicy(enabled=False).selected_layout().value == "legacy"
