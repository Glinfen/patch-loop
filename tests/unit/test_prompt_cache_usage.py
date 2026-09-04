from __future__ import annotations

import pytest

from patchloop.prompt_cache import CacheUsageAccumulator, CacheUsageAccumulatorSnapshot
from patchloop.providers import ModelUsage


def test_accumulator_distinguishes_unreported_usage_from_reported_zero() -> None:
    unknown = CacheUsageAccumulator()
    unknown.record(ModelUsage(input_tokens=12))
    zero = CacheUsageAccumulator()
    zero.record(
        ModelUsage(
            input_tokens=0,
            cache_hit_tokens=0,
            cache_miss_tokens=0,
            cache_write_tokens=0,
        )
    )

    assert unknown.report_fields() == {
        "cache_hit_tokens": None,
        "cache_miss_tokens": None,
        "cache_write_tokens": None,
        "cache_hit_rate": None,
        "cache_usage_reported_calls": 0,
        "cache_usage_unreported_calls": 1,
        "cache_usage_inconsistent_calls": 0,
        "cache_write_reported_calls": 0,
    }
    assert zero.report_fields() == {
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "cache_write_tokens": 0,
        "cache_hit_rate": None,
        "cache_usage_reported_calls": 1,
        "cache_usage_unreported_calls": 0,
        "cache_usage_inconsistent_calls": 0,
        "cache_write_reported_calls": 1,
    }
    assert unknown.snapshot().cache_hit_tokens is None
    assert zero.snapshot().cache_hit_tokens == 0


def test_accumulator_handles_mixed_usage_partial_fields_and_write_only_calls() -> None:
    accumulator = CacheUsageAccumulator()
    accumulator.record(
        ModelUsage(
            input_tokens=100,
            cache_hit_tokens=80,
            cache_miss_tokens=20,
            cache_write_tokens=3,
        )
    )
    accumulator.record(ModelUsage(input_tokens=50))
    accumulator.record(ModelUsage(input_tokens=30, cache_hit_tokens=10, cache_miss_tokens=25))
    accumulator.record(ModelUsage(input_tokens=40, cache_hit_tokens=0, cache_miss_tokens=0))
    accumulator.record(ModelUsage(input_tokens=5, cache_write_tokens=4))

    assert accumulator.report_fields() == {
        "cache_hit_tokens": 90,
        "cache_miss_tokens": 45,
        "cache_write_tokens": 7,
        "cache_hit_rate": 90 / 135,
        "cache_usage_reported_calls": 3,
        "cache_usage_unreported_calls": 2,
        "cache_usage_inconsistent_calls": 2,
        "cache_write_reported_calls": 2,
    }
    assert accumulator.checkpoint_fields() == {
        "cache_hit_tokens": 90,
        "cache_miss_tokens": 45,
        "cache_write_tokens": 7,
        "cache_usage_reported_calls": 3,
        "cache_usage_unreported_calls": 2,
        "cache_usage_inconsistent_calls": 2,
        "cache_write_reported_calls": 2,
    }


def test_accumulator_snapshot_and_legacy_checkpoint_restore_continue_accumulating() -> None:
    accumulator = CacheUsageAccumulator()
    accumulator.record(ModelUsage(input_tokens=10, cache_hit_tokens=6, cache_miss_tokens=4))
    restored = CacheUsageAccumulator.from_snapshot(accumulator.snapshot())
    restored.record(ModelUsage(input_tokens=20, cache_hit_tokens=15, cache_miss_tokens=5))

    legacy = CacheUsageAccumulator.from_legacy(
        cache_hit_tokens=6,
        cache_miss_tokens=4,
        cache_write_tokens=0,
        cache_usage_reported_calls=1,
        cache_usage_unreported_calls=2,
        cache_usage_inconsistent_calls=1,
        cache_write_reported_calls=0,
    )
    legacy.record(ModelUsage(input_tokens=20, cache_hit_tokens=20, cache_miss_tokens=0))

    assert restored.report_fields()["cache_hit_tokens"] == 21
    assert restored.report_fields()["cache_miss_tokens"] == 9
    assert legacy.report_fields() == {
        "cache_hit_tokens": 26,
        "cache_miss_tokens": 4,
        "cache_write_tokens": None,
        "cache_hit_rate": 26 / 30,
        "cache_usage_reported_calls": 2,
        "cache_usage_unreported_calls": 2,
        "cache_usage_inconsistent_calls": 1,
        "cache_write_reported_calls": 0,
    }


def test_snapshot_rejects_ambiguous_reported_and_missing_values() -> None:
    with pytest.raises(ValueError, match="hit and miss totals"):
        CacheUsageAccumulatorSnapshot(cache_usage_reported_calls=1)
    with pytest.raises(ValueError, match="write total"):
        CacheUsageAccumulatorSnapshot(cache_write_reported_calls=1)
