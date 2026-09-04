"""Cache usage accumulation independent from provider and runtime lifecycles."""

from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.providers.base import ModelUsage


class CacheUsageAccumulatorSnapshot(BaseModel):
    """Checkpoint-safe cache usage totals.

    Optional token totals intentionally distinguish a provider that never
    reported cache usage from a provider that reported a zero value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    cache_usage_reported_calls: int = Field(default=0, ge=0)
    cache_usage_unreported_calls: int = Field(default=0, ge=0)
    cache_usage_inconsistent_calls: int = Field(default=0, ge=0)
    cache_write_reported_calls: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_reported_values(self) -> CacheUsageAccumulatorSnapshot:
        cache_values_reported = self.cache_usage_reported_calls > 0
        if cache_values_reported != (
            self.cache_hit_tokens is not None and self.cache_miss_tokens is not None
        ):
            raise ValueError(
                "cache hit and miss totals must be present exactly when usage is reported"
            )
        write_reported = self.cache_write_reported_calls > 0
        if write_reported != (self.cache_write_tokens is not None):
            raise ValueError("cache write total must be present exactly when writes are reported")
        return self


class CacheUsageReportFields(TypedDict):
    cache_hit_tokens: int | None
    cache_miss_tokens: int | None
    cache_write_tokens: int | None
    cache_hit_rate: float | None
    cache_usage_reported_calls: int
    cache_usage_unreported_calls: int
    cache_usage_inconsistent_calls: int
    cache_write_reported_calls: int


class CacheUsageCheckpointFields(TypedDict):
    cache_hit_tokens: int
    cache_miss_tokens: int
    cache_write_tokens: int
    cache_usage_reported_calls: int
    cache_usage_unreported_calls: int
    cache_usage_inconsistent_calls: int
    cache_write_reported_calls: int


class CacheUsageAccumulator:
    """Accumulate cache fields while preserving missing values as unknown."""

    def __init__(self, snapshot: CacheUsageAccumulatorSnapshot | None = None) -> None:
        self.reset()
        if snapshot is not None:
            self._restore(snapshot)

    def reset(self) -> None:
        self._cache_hit_tokens: int | None = None
        self._cache_miss_tokens: int | None = None
        self._cache_write_tokens: int | None = None
        self._cache_usage_reported_calls = 0
        self._cache_usage_unreported_calls = 0
        self._cache_usage_inconsistent_calls = 0
        self._cache_write_reported_calls = 0

    @classmethod
    def from_snapshot(cls, snapshot: CacheUsageAccumulatorSnapshot) -> CacheUsageAccumulator:
        return cls(snapshot)

    @classmethod
    def from_legacy(
        cls,
        *,
        cache_hit_tokens: int,
        cache_miss_tokens: int,
        cache_write_tokens: int,
        cache_usage_reported_calls: int,
        cache_usage_unreported_calls: int,
        cache_usage_inconsistent_calls: int,
        cache_write_reported_calls: int,
    ) -> CacheUsageAccumulator:
        """Restore pre-PCR-02 checkpoint fields without conflating unknowns."""

        return cls(
            CacheUsageAccumulatorSnapshot(
                cache_hit_tokens=(cache_hit_tokens if cache_usage_reported_calls else None),
                cache_miss_tokens=(cache_miss_tokens if cache_usage_reported_calls else None),
                cache_write_tokens=(cache_write_tokens if cache_write_reported_calls else None),
                cache_usage_reported_calls=cache_usage_reported_calls,
                cache_usage_unreported_calls=cache_usage_unreported_calls,
                cache_usage_inconsistent_calls=cache_usage_inconsistent_calls,
                cache_write_reported_calls=cache_write_reported_calls,
            )
        )

    def record(self, usage: ModelUsage) -> None:
        hit_tokens = usage.cache_hit_tokens
        miss_tokens = usage.cache_miss_tokens
        if hit_tokens is None and miss_tokens is None:
            self._cache_usage_unreported_calls += 1
        elif hit_tokens is None or miss_tokens is None:
            self._cache_usage_unreported_calls += 1
            self._cache_usage_inconsistent_calls += 1
        else:
            self._cache_usage_reported_calls += 1
            self._cache_hit_tokens = (self._cache_hit_tokens or 0) + hit_tokens
            self._cache_miss_tokens = (self._cache_miss_tokens or 0) + miss_tokens
            if hit_tokens + miss_tokens != usage.input_tokens:
                self._cache_usage_inconsistent_calls += 1
        if usage.cache_write_tokens is not None:
            self._cache_write_tokens = (self._cache_write_tokens or 0) + usage.cache_write_tokens
            self._cache_write_reported_calls += 1

    def snapshot(self) -> CacheUsageAccumulatorSnapshot:
        return CacheUsageAccumulatorSnapshot(
            cache_hit_tokens=self._cache_hit_tokens,
            cache_miss_tokens=self._cache_miss_tokens,
            cache_write_tokens=self._cache_write_tokens,
            cache_usage_reported_calls=self._cache_usage_reported_calls,
            cache_usage_unreported_calls=self._cache_usage_unreported_calls,
            cache_usage_inconsistent_calls=self._cache_usage_inconsistent_calls,
            cache_write_reported_calls=self._cache_write_reported_calls,
        )

    def report_fields(self) -> CacheUsageReportFields:
        return {
            "cache_hit_tokens": self._cache_hit_tokens,
            "cache_miss_tokens": self._cache_miss_tokens,
            "cache_write_tokens": self._cache_write_tokens,
            "cache_hit_rate": self.cache_hit_rate,
            "cache_usage_reported_calls": self._cache_usage_reported_calls,
            "cache_usage_unreported_calls": self._cache_usage_unreported_calls,
            "cache_usage_inconsistent_calls": self._cache_usage_inconsistent_calls,
            "cache_write_reported_calls": self._cache_write_reported_calls,
        }

    def checkpoint_fields(self) -> CacheUsageCheckpointFields:
        return {
            "cache_hit_tokens": self._cache_hit_tokens or 0,
            "cache_miss_tokens": self._cache_miss_tokens or 0,
            "cache_write_tokens": self._cache_write_tokens or 0,
            "cache_usage_reported_calls": self._cache_usage_reported_calls,
            "cache_usage_unreported_calls": self._cache_usage_unreported_calls,
            "cache_usage_inconsistent_calls": self._cache_usage_inconsistent_calls,
            "cache_write_reported_calls": self._cache_write_reported_calls,
        }

    @property
    def cache_hit_rate(self) -> float | None:
        cache_tokens = (self._cache_hit_tokens or 0) + (self._cache_miss_tokens or 0)
        if not self._cache_usage_reported_calls or cache_tokens == 0:
            return None
        return (self._cache_hit_tokens or 0) / cache_tokens

    def _restore(self, snapshot: CacheUsageAccumulatorSnapshot) -> None:
        self._cache_hit_tokens = snapshot.cache_hit_tokens
        self._cache_miss_tokens = snapshot.cache_miss_tokens
        self._cache_write_tokens = snapshot.cache_write_tokens
        self._cache_usage_reported_calls = snapshot.cache_usage_reported_calls
        self._cache_usage_unreported_calls = snapshot.cache_usage_unreported_calls
        self._cache_usage_inconsistent_calls = snapshot.cache_usage_inconsistent_calls
        self._cache_write_reported_calls = snapshot.cache_write_reported_calls


__all__ = [
    "CacheUsageAccumulator",
    "CacheUsageAccumulatorSnapshot",
    "CacheUsageCheckpointFields",
    "CacheUsageReportFields",
]
