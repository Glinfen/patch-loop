"""Metrics and deterministic replay derived from append-only task traces."""

from __future__ import annotations

from collections import Counter
from contextlib import suppress
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from patchloop.events import Event
from patchloop.prompt_cache import CacheLayoutTrace


class TaskMetrics(BaseModel):
    task_id: str
    status: str = "unknown"
    events: int = Field(default=0, ge=0)
    steps: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    failed_tool_calls: int = Field(default=0, ge=0)
    approvals_requested: int = Field(default=0, ge=0)
    approvals_denied: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    cache_hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    cache_usage_reported_calls: int = Field(default=0, ge=0)
    cache_usage_unreported_calls: int = Field(default=0, ge=0)
    cache_usage_inconsistent_calls: int = Field(default=0, ge=0)
    cache_write_reported_calls: int = Field(default=0, ge=0)
    cache_layout_events: int = Field(default=0, ge=0)
    cache_layout_fingerprint_changes: int = Field(default=0, ge=0)
    cache_layout_reason_counts: dict[str, int] = Field(default_factory=dict)
    cache_layout_primary_reasons: dict[str, int] = Field(default_factory=dict)
    cache_layout_first_change_sections: dict[str, int] = Field(default_factory=dict)
    cache_stable_prefix_tokens: int = Field(default=0, ge=0)
    cache_longest_common_prefix_tokens: int = Field(default=0, ge=0)
    tool_duration_ms: float = Field(default=0.0, ge=0)
    elapsed_ms: float = Field(default=0.0, ge=0)
    working_memory_updates: int = Field(default=0, ge=0)
    working_memory_evictions: int = Field(default=0, ge=0)
    memory_promotions: int = Field(default=0, ge=0)
    max_working_memory_tokens_used: int = Field(default=0, ge=0)
    episodes_created: int = Field(default=0, ge=0)
    episode_recoveries: int = Field(default=0, ge=0)
    repeated_failed_actions_blocked: int = Field(default=0, ge=0)
    semantic_facts_created: int = Field(default=0, ge=0)
    semantic_facts_superseded: int = Field(default=0, ge=0)
    semantic_conflicts_rejected: int = Field(default=0, ge=0)
    semantic_duplicates_suppressed: int = Field(default=0, ge=0)
    memory_retrievals: int = Field(default=0, ge=0)
    memory_retrieval_hits: int = Field(default=0, ge=0)
    memory_retrieval_tokens: int = Field(default=0, ge=0)
    memory_records_written: int = Field(default=0, ge=0)
    memory_records_superseded: int = Field(default=0, ge=0)
    memory_compactions: int = Field(default=0, ge=0)
    memory_fallbacks: int = Field(default=0, ge=0)
    memory_replays: int = Field(default=0, ge=0)
    memory_stale_hits: int = Field(default=0, ge=0)
    memory_security_filters: int = Field(default=0, ge=0)
    memory_compression_input_tokens: int = Field(default=0, ge=0)
    memory_compression_output_tokens: int = Field(default=0, ge=0)
    memory_compression_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    memory_read_duration_ms: float = Field(default=0.0, ge=0.0)
    memory_write_duration_ms: float = Field(default=0.0, ge=0.0)
    memory_compression_duration_ms: float = Field(default=0.0, ge=0.0)
    max_memory_context_tokens_used: int = Field(default=0, ge=0)
    max_memory_context_occupancy: float = Field(default=0.0, ge=0.0, le=1.0)
    memory_records_by_kind: dict[str, int] = Field(default_factory=dict)
    memory_records_by_status: dict[str, int] = Field(default_factory=dict)
    errors: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def from_events(cls, task_id: str, events: list[Event]) -> TaskMetrics:
        selected = [event for event in events if event.task_id == task_id]
        metrics = cls(task_id=task_id, events=len(selected))
        step_indices: set[int] = set()
        errors: Counter[str] = Counter()
        for event in selected:
            if event.type == "step.started" and isinstance(event.data.get("step"), int):
                step_indices.add(event.data["step"])
            elif event.type == "model.completed":
                metrics.model_calls += 1
                usage = event.data.get("usage", {})
                if isinstance(usage, dict):
                    metrics.input_tokens += int(usage.get("input_tokens", 0))
                    metrics.output_tokens += int(usage.get("output_tokens", 0))
                    metrics.cost_usd += float(usage.get("cost_usd", 0.0))
                    _apply_provider_usage(metrics, usage)
            elif event.type == "cache.layout":
                try:
                    layout = CacheLayoutTrace.model_validate(event.data)
                except ValueError:
                    continue
                metrics.cache_layout_events += 1
                if layout.first_change_section is not None:
                    metrics.cache_layout_fingerprint_changes += 1
                    section = layout.first_change_section
                    metrics.cache_layout_first_change_sections[section] = (
                        metrics.cache_layout_first_change_sections.get(section, 0) + 1
                    )
                for reason in layout.reasons:
                    key = reason.value
                    metrics.cache_layout_reason_counts[key] = (
                        metrics.cache_layout_reason_counts.get(key, 0) + 1
                    )
                primary = layout.primary_reason.value
                metrics.cache_layout_primary_reasons[primary] = (
                    metrics.cache_layout_primary_reasons.get(primary, 0) + 1
                )
                metrics.cache_stable_prefix_tokens = max(
                    metrics.cache_stable_prefix_tokens,
                    layout.stable_prefix_tokens,
                )
                metrics.cache_longest_common_prefix_tokens = max(
                    metrics.cache_longest_common_prefix_tokens,
                    layout.longest_common_prefix_tokens,
                )
            elif event.type == "tool.completed":
                metrics.tool_calls += 1
                result = event.data.get("result", {})
                if isinstance(result, dict):
                    metrics.tool_duration_ms += float(result.get("duration_ms", 0.0))
                    if not result.get("success", False):
                        metrics.failed_tool_calls += 1
                        errors[str(result.get("error_kind") or "unknown")] += 1
            elif event.type == "security.decision":
                assessment = event.data.get("assessment", {})
                if isinstance(assessment, dict) and assessment.get("approval_required"):
                    metrics.approvals_requested += 1
                    if not assessment.get("allowed"):
                        metrics.approvals_denied += 1
            elif event.type == "working_memory.updated":
                metrics.working_memory_updates += 1
                metrics.working_memory_evictions = max(
                    metrics.working_memory_evictions,
                    int(event.data.get("evicted_count", 0)),
                )
                metrics.max_working_memory_tokens_used = max(
                    metrics.max_working_memory_tokens_used,
                    int(event.data.get("estimated_tokens", 0)),
                )
            elif event.type == "memory.promoted":
                metrics.memory_promotions += int(event.data.get("records", 0))
            elif event.type == "episode.created":
                metrics.episodes_created += 1
                if event.data.get("recovers_episode_ids"):
                    metrics.episode_recoveries += 1
            elif event.type == "episode.repeat_blocked":
                metrics.repeated_failed_actions_blocked += 1
            elif event.type == "semantic.facts_resolved":
                metrics.semantic_facts_created += int(event.data.get("created", 0))
                metrics.semantic_facts_superseded += int(event.data.get("superseded", 0))
                metrics.semantic_conflicts_rejected += int(event.data.get("conflicts_rejected", 0))
                metrics.semantic_duplicates_suppressed += int(
                    event.data.get("duplicates_suppressed", 0)
                )
            elif event.type == "memory.retrieved":
                metrics.memory_retrievals += 1
                raw_selected = event.data.get("selected", [])
                if isinstance(raw_selected, list):
                    metrics.memory_retrieval_hits += sum(
                        isinstance(item, dict) and item.get("record_id") is not None
                        for item in raw_selected
                    )
                metrics.memory_retrieval_tokens += int(event.data.get("estimated_tokens", 0))
                metrics.memory_stale_hits += int(event.data.get("stale_hits", 0))
                metrics.memory_read_duration_ms += float(event.data.get("read_duration_ms", 0.0))
                metrics.max_memory_context_tokens_used = max(
                    metrics.max_memory_context_tokens_used,
                    int(event.data.get("estimated_tokens", 0)),
                )
                metrics.max_memory_context_occupancy = max(
                    metrics.max_memory_context_occupancy,
                    float(event.data.get("context_occupancy", 0.0)),
                )
            elif event.type == "memory.written":
                record_ids = event.data.get("record_ids", [])
                if isinstance(record_ids, list):
                    metrics.memory_records_written += len(record_ids)
                metrics.memory_write_duration_ms += float(event.data.get("write_duration_ms", 0.0))
                _apply_memory_inventory(metrics, event.data.get("inventory"))
            elif event.type == "memory.superseded":
                records = event.data.get("records", [])
                if isinstance(records, list):
                    metrics.memory_records_superseded += len(records)
            elif event.type == "memory.compacted":
                metrics.memory_compactions += 1
                written_record_ids = event.data.get("written_record_ids", [])
                if isinstance(written_record_ids, list):
                    metrics.memory_records_written += len(written_record_ids)
                metrics.memory_compression_input_tokens += int(event.data.get("input_tokens", 0))
                metrics.memory_compression_output_tokens += int(event.data.get("output_tokens", 0))
                metrics.memory_compression_duration_ms += float(event.data.get("duration_ms", 0.0))
                _apply_memory_inventory(metrics, event.data.get("inventory"))
            elif event.type == "memory.fallback":
                metrics.memory_fallbacks += 1
            elif event.type == "memory.replayed":
                metrics.memory_replays += 1
            elif event.type == "memory.security_filtered":
                selections = event.data.get("selections", [])
                if isinstance(selections, list):
                    metrics.memory_security_filters += sum(
                        len(item.get("findings", []))
                        for item in selections
                        if isinstance(item, dict) and isinstance(item.get("findings"), list)
                    )
            elif event.type in {"task.completed", "task.failed", "task.cancelled"}:
                metrics.status = event.type.removeprefix("task.")
                if event.type == "task.failed":
                    errors[str(event.data.get("error_kind") or "unknown")] += 1
        metrics.steps = len(step_indices)
        metrics.errors = dict(sorted(errors.items()))
        if metrics.memory_compression_input_tokens:
            metrics.memory_compression_ratio = (
                metrics.memory_compression_output_tokens / metrics.memory_compression_input_tokens
            )
        cache_tokens = (metrics.cache_hit_tokens or 0) + (metrics.cache_miss_tokens or 0)
        if metrics.cache_usage_reported_calls and cache_tokens:
            metrics.cache_hit_rate = (metrics.cache_hit_tokens or 0) / cache_tokens
        if len(selected) >= 2:
            metrics.elapsed_ms = max(
                0.0,
                (selected[-1].timestamp - selected[0].timestamp).total_seconds() * 1_000,
            )
        return metrics


class ReplayFrame(BaseModel):
    sequence: int
    timestamp: datetime
    type: str
    step: int | None = None
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class MemoryDecision(BaseModel):
    sequence: int
    step: int
    query: str
    selected_record_ids: list[str] = Field(default_factory=list)
    selections: list[dict[str, Any]] = Field(default_factory=list)
    omitted_ids: list[str] = Field(default_factory=list)
    estimated_tokens: int = Field(default=0, ge=0)
    context_occupancy: float = Field(default=0.0, ge=0.0, le=1.0)


class ProviderUsageRecord(BaseModel):
    sequence: int
    step: int
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    cache_hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    cache_usage_consistent: bool | None = None


class TaskReplay(BaseModel):
    task_id: str
    frames: list[ReplayFrame]
    memory_decisions: list[MemoryDecision] = Field(default_factory=list)
    provider_usages: list[ProviderUsageRecord] = Field(default_factory=list)
    cache_layouts: list[CacheLayoutTrace] = Field(default_factory=list)

    @classmethod
    def from_events(cls, task_id: str, events: list[Event]) -> TaskReplay:
        frames = []
        memory_decisions: list[MemoryDecision] = []
        provider_usages: list[ProviderUsageRecord] = []
        cache_layouts: list[CacheLayoutTrace] = []
        current_step: int | None = None
        for event in events:
            if event.task_id != task_id:
                continue
            event_step = event.data.get("step")
            if event.type == "step.started" and isinstance(event_step, int):
                current_step = event_step
            frames.append(
                ReplayFrame(
                    sequence=event.sequence,
                    timestamp=event.timestamp,
                    type=event.type,
                    step=event_step if isinstance(event_step, int) else current_step,
                    summary=_event_summary(event),
                    data=event.data,
                )
            )
            if event.type == "memory.retrieved" and isinstance(event_step, int):
                raw_selections = event.data.get("selected", [])
                selections = (
                    [item for item in raw_selections if isinstance(item, dict)]
                    if isinstance(raw_selections, list)
                    else []
                )
                memory_decisions.append(
                    MemoryDecision(
                        sequence=event.sequence,
                        step=event_step,
                        query=str(event.data.get("query", "")),
                        selected_record_ids=[
                            str(item["record_id"])
                            for item in selections
                            if item.get("record_id") is not None
                        ],
                        selections=selections,
                        omitted_ids=[str(item) for item in event.data.get("omitted_ids", [])],
                        estimated_tokens=int(event.data.get("estimated_tokens", 0)),
                        context_occupancy=float(event.data.get("context_occupancy", 0.0)),
                    )
                )
            if event.type == "cache.layout":
                with suppress(ValueError):
                    cache_layouts.append(CacheLayoutTrace.model_validate(event.data))
            if event.type == "model.completed" and isinstance(event_step, int):
                usage = event.data.get("usage", {})
                if isinstance(usage, dict):
                    input_tokens = _nonnegative_int(usage.get("input_tokens"))
                    hit_tokens = _optional_nonnegative_int(usage.get("cache_hit_tokens"))
                    miss_tokens = _optional_nonnegative_int(usage.get("cache_miss_tokens"))
                    cache_tokens = (hit_tokens or 0) + (miss_tokens or 0)
                    if hit_tokens is not None and miss_tokens is not None:
                        cache_hit_rate = hit_tokens / cache_tokens if cache_tokens else None
                        cache_usage_consistent = hit_tokens + miss_tokens == input_tokens
                    else:
                        cache_hit_rate = None
                        cache_usage_consistent = None
                    provider_usages.append(
                        ProviderUsageRecord(
                            sequence=event.sequence,
                            step=event_step,
                            input_tokens=input_tokens,
                            output_tokens=_nonnegative_int(usage.get("output_tokens")),
                            cost_usd=_nonnegative_float(usage.get("cost_usd")),
                            cache_hit_tokens=hit_tokens,
                            cache_miss_tokens=miss_tokens,
                            cache_write_tokens=_optional_nonnegative_int(
                                usage.get("cache_write_tokens")
                            ),
                            cache_hit_rate=cache_hit_rate,
                            cache_usage_consistent=cache_usage_consistent,
                        )
                    )
            if event.type == "step.completed":
                current_step = None
        return cls(
            task_id=task_id,
            frames=frames,
            memory_decisions=memory_decisions,
            provider_usages=provider_usages,
            cache_layouts=cache_layouts,
        )


def _apply_provider_usage(metrics: TaskMetrics, usage: dict[str, Any]) -> None:
    hit_tokens = _optional_nonnegative_int(usage.get("cache_hit_tokens"))
    miss_tokens = _optional_nonnegative_int(usage.get("cache_miss_tokens"))
    input_tokens = _nonnegative_int(usage.get("input_tokens"))
    if hit_tokens is None and miss_tokens is None:
        metrics.cache_usage_unreported_calls += 1
    elif hit_tokens is None or miss_tokens is None:
        metrics.cache_usage_unreported_calls += 1
        metrics.cache_usage_inconsistent_calls += 1
    else:
        metrics.cache_usage_reported_calls += 1
        metrics.cache_hit_tokens = (metrics.cache_hit_tokens or 0) + hit_tokens
        metrics.cache_miss_tokens = (metrics.cache_miss_tokens or 0) + miss_tokens
        if hit_tokens + miss_tokens != input_tokens:
            metrics.cache_usage_inconsistent_calls += 1
    write_tokens = _optional_nonnegative_int(usage.get("cache_write_tokens"))
    if write_tokens is not None:
        metrics.cache_write_tokens = (metrics.cache_write_tokens or 0) + write_tokens
        metrics.cache_write_reported_calls += 1


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _optional_nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _nonnegative_float(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return 0.0


def _apply_memory_inventory(metrics: TaskMetrics, raw_inventory: object) -> None:
    if not isinstance(raw_inventory, dict):
        return
    by_kind = raw_inventory.get("by_kind", {})
    by_status = raw_inventory.get("by_status", {})
    if isinstance(by_kind, dict):
        metrics.memory_records_by_kind = {str(key): int(value) for key, value in by_kind.items()}
    if isinstance(by_status, dict):
        metrics.memory_records_by_status = {
            str(key): int(value) for key, value in by_status.items()
        }


def _event_summary(event: Event) -> str:
    if event.type == "tool.completed":
        result = event.data.get("result", {})
        if isinstance(result, dict):
            state = "passed" if result.get("success") else "failed"
            return f"{result.get('tool_name', 'tool')} {state}"
    if event.type == "security.decision":
        assessment = event.data.get("assessment", {})
        if isinstance(assessment, dict):
            return f"{assessment.get('risk', 'unknown')} risk: {assessment.get('reason', '')}"
    if event.type == "memory.retrieved":
        selected = event.data.get("selected", [])
        count = len(selected) if isinstance(selected, list) else 0
        return f"memory decision selected {count} items"
    if event.type == "memory.replayed":
        return f"memory replay suppressed {event.data.get('event_id', 'event')}"
    if event.type == "cache.layout":
        return f"cache layout: {event.data.get('primary_reason', 'unknown')}"
    if event.type.startswith("task."):
        return event.type.replace(".", " ")
    if event.type.startswith("step."):
        return f"step {event.data.get('step', '?')} {event.type.removeprefix('step.')}"
    return event.type.replace(".", " ")
