from datetime import UTC, datetime, timedelta

import pytest

from patchloop.evaluation import (
    CacheAcceptanceEvaluator,
    CacheBenchmarkRunner,
    CacheRolloutPolicy,
    GateStatus,
    MemoryQualityEvidence,
    RealProviderCacheCollector,
)
from patchloop.events import Event
from patchloop.prompt_cache import CacheLayoutTrace
from patchloop.providers import ModelUsage


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


def test_aop01_provider_purpose_totals_separate_rollover_failure_and_unknown_usage() -> None:
    started = datetime(2026, 9, 15, tzinfo=UTC)
    events: list[Event] = []
    request_specs = (
        ("ordinary", "agent_step", 100, 0.1),
        ("compression-success", "epoch_compression", 200, 0.2),
        ("compression-no-rollover", "epoch_compression", 300, 0.3),
    )
    for index, (request_id, purpose, input_tokens, cost) in enumerate(request_specs):
        attempt_id = f"attempt-{index}"
        usage = ModelUsage(
            input_tokens=input_tokens,
            output_tokens=10,
            cache_hit_tokens=input_tokens * 4 // 5,
            cache_miss_tokens=input_tokens // 5,
            cost_usd=cost,
            cost_status="estimated",
            input_tokens_reported=True,
            output_tokens_reported=True,
        )
        trace = CacheLayoutTrace(
            step=index,
            request_id=request_id,
            provider="real-test",
            request_fingerprint=f"{index + 1:x}" * 64,
            comparison_kind="compression" if purpose == "epoch_compression" else "ordinary",
            metric_basis="normalized_messages_v1",
            cache_hit_tokens=usage.cache_hit_tokens,
            cache_miss_tokens=usage.cache_miss_tokens,
            cache_usage_consistent=True,
        )
        events.extend(
            [
                Event(
                    id=f"request-started-{index}",
                    type="provider.request.started",
                    task_id="task",
                    timestamp=started + timedelta(seconds=index * 3),
                    data={"request_id": request_id, "purpose": purpose},
                ),
                Event(
                    id=f"attempt-started-{index}",
                    type="provider.attempt.started",
                    task_id="task",
                    timestamp=started + timedelta(seconds=index * 3 + 1),
                    data={"request_id": request_id, "attempt_id": attempt_id},
                ),
                Event(
                    id=f"request-completed-{index}",
                    type="provider.request.completed",
                    task_id="task",
                    timestamp=started + timedelta(seconds=index * 3 + 2),
                    data={
                        "request_id": request_id,
                        "attempt_id": attempt_id,
                        "purpose": purpose,
                        "usage": usage.model_dump(mode="json"),
                    },
                ),
                Event(
                    id=f"layout-{index}",
                    type="cache.layout",
                    task_id="task",
                    data=trace.model_dump(mode="json"),
                ),
            ]
        )
    events.append(
        Event(
            id="rollover-success",
            type="cache.epoch.rolled_over",
            task_id="task",
            data={
                "request_id": "compression-success",
                "optimization_version": "baseline_v1",
                "projection_format": "legacy_v1",
                "new_memory_tokens": 500,
                "working_item_count": 1,
                "opaque_working_blob_count": 1,
                "decision_reason": "soft_limit_exceeded",
                "summary_estimated_tokens": 80,
                "freed_input_tokens": 1200,
                "headroom_after_rebase": 900,
            },
        )
    )
    events.append(
        Event(
            id="compression-requested",
            type="cache.compression.requested",
            task_id="task",
            data={
                "request_id": "compression-success",
                "new_memory_tokens": 500,
            },
        )
    )
    events.extend(
        [
            Event(
                id="request-started-unknown",
                type="provider.request.started",
                task_id="task",
                timestamp=started + timedelta(seconds=10),
                data={
                    "request_id": "compression-cancelled",
                    "purpose": "epoch_compression",
                },
            ),
            Event(
                id="attempt-started-unknown",
                type="provider.attempt.started",
                task_id="task",
                timestamp=started + timedelta(seconds=11),
                data={
                    "request_id": "compression-cancelled",
                    "attempt_id": "attempt-unknown",
                },
            ),
            Event(
                id="attempt-finished-unknown",
                type="provider.attempt.finished",
                task_id="task",
                timestamp=started + timedelta(seconds=12),
                data={
                    "request_id": "compression-cancelled",
                    "attempt_id": "attempt-unknown",
                    "status": "failed",
                    "request_sent": True,
                    "usage_unknown": True,
                },
            ),
        ]
    )
    # Replayed JSONL records keep their event IDs and must not inflate totals.
    events.extend([events[5], events[-1]])

    report = RealProviderCacheCollector.run_from_events(events)

    assert report.compression_request_count == 3
    assert report.compression_count == 3
    assert report.successful_rollover_count == 1
    assert report.failed_compression_count == 2
    assert report.unknown_usage_attempts == 1
    assert report.cost_usd is None
    assert report.known_totals is not None
    assert report.known_totals.input_tokens == 600
    assert report.known_totals.cost_usd == pytest.approx(0.6)
    compression = report.purpose_summaries["epoch_compression"]
    assert compression.request_count == 3
    assert compression.completed_request_count == 2
    assert compression.attempt_count == 3
    assert compression.unknown_usage_attempts == 1
    assert compression.input_tokens == 500
    assert compression.cost_usd is None
    assert report.optimization_version == "baseline_v1"
    assert report.opaque_working_blob_count == 1
    assert report.summary_estimated_tokens == 80
    assert report.freed_input_tokens == 1200


def test_aop01_old_cache_run_report_defaults_new_diagnostics_to_none() -> None:
    current = CacheBenchmarkRunner(repeats=3).run().runs[0]
    payload = current.model_dump(
        exclude={
            "purpose_summaries",
            "known_totals",
            "compression_request_count",
            "successful_rollover_count",
            "failed_compression_count",
            "optimization_version",
            "projection_format",
            "projection_fallback_reason",
            "new_memory_tokens",
            "working_item_count",
            "opaque_working_blob_count",
            "decision_reason",
            "mandatory_rebase_tokens",
            "summary_target_tokens",
            "summary_estimated_tokens",
            "freed_input_tokens",
            "headroom_after_rebase",
        }
    )

    restored = type(current).model_validate(payload)

    assert restored.compression_request_count is None
    assert restored.successful_rollover_count is None
    assert restored.known_totals is None
    assert restored.optimization_version is None
    assert restored.opaque_working_blob_count is None
