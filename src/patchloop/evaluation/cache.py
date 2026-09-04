"""Cache-aware deterministic and provider-reported evaluation.

The deterministic suite measures request layout, not a response cache.  Real
provider runs are intentionally kept separate: hit/miss values are copied from
the provider event trace and are never replaced with local estimates.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from enum import StrEnum
from itertools import pairwise
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.events import Event
from patchloop.prompt_cache import (
    CacheEpoch,
    CacheEpochBoundary,
    CacheLayoutReason,
    CacheLayoutTrace,
)
from patchloop.providers import DeterministicPrefixCacheSimulator
from patchloop.providers.base import ModelMessage, ModelUsage, ToolSpec


class CacheEvaluationVariant(StrEnum):
    CURRENT_LAYOUT = "current_layout"
    STABLE_PREFIX = "stable_prefix"
    FROZEN_TOOLS = "frozen_tools"
    PROVIDER_PROJECTION = "provider_projection"
    EPOCH_COMPRESSION = "epoch_compression"
    FULL_OPTIMIZATION = "full_optimization"


class CacheEvaluationScenario(StrEnum):
    COLD_START = "cold_start"
    WARM_CONTINUATION = "warm_continuation"
    PERMISSION_CHANGE = "permission_change"
    PROJECT_CHANGE = "project_change"
    EXPLICIT_COMPRESSION = "explicit_compression"
    CHECKPOINT_RESTORE = "checkpoint_restore"
    MODEL_SWITCH = "model_switch"


ALL_CACHE_VARIANTS: tuple[CacheEvaluationVariant, ...] = tuple(CacheEvaluationVariant)
ALL_CACHE_SCENARIOS: tuple[CacheEvaluationScenario, ...] = tuple(CacheEvaluationScenario)


class CacheSimulationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step: int = Field(ge=0)
    scenario: CacheEvaluationScenario
    messages: list[ModelMessage]
    tools: list[ToolSpec]
    provider: str = "fake"
    model: str = "deepseek-v4-flash"
    thinking: dict[str, Any] = Field(default_factory=lambda: {"enabled": True})
    epoch_snapshot: object = "epoch-1"
    system_instructions: str | None = None
    task_project_snapshot: object | None = None
    memory_projection: object | None = None
    expected_full_invalidation: bool = False


class CacheSimulationStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step: int = Field(ge=0)
    scenario: CacheEvaluationScenario
    input_tokens: int | None = Field(default=None, ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    cache_hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    longest_common_prefix_tokens: int = Field(default=0, ge=0)
    longest_common_prefix_bytes: int = Field(default=0, ge=0)
    stable_prefix_tokens: int = Field(default=0, ge=0)
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    first_change_section: str | None = None
    primary_reason: CacheLayoutReason = CacheLayoutReason.UNKNOWN
    reasons: list[CacheLayoutReason] = Field(default_factory=list)
    expected_full_invalidation: bool = False
    latency_ms: float | None = Field(default=None, ge=0.0)
    cost_usd: float | None = Field(default=None, ge=0.0)


class CacheRunReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    variant: CacheEvaluationVariant
    repeat: int = Field(ge=1)
    source: str = "deterministic_simulation"
    provider: str = "fake"
    task_correctness: float = Field(ge=0.0, le=1.0)
    steps: list[CacheSimulationStep] = Field(min_length=1)
    input_tokens: int | None = Field(default=None, ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    cache_hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    steady_state_cache_hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    latency_ms: float | None = Field(default=None, ge=0.0)
    cost_usd: float | None = Field(default=None, ge=0.0)
    compression_count: int = Field(default=0, ge=0)
    prefix_rotations: int = Field(default=0, ge=0)
    fingerprint_changes: int = Field(default=0, ge=0)
    large_miss_attributions: int = Field(default=0, ge=0)
    reason_counts: dict[str, int] = Field(default_factory=dict)


class CacheVariantSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    variant: CacheEvaluationVariant
    run_count: int = Field(ge=1)
    task_correctness_mean: float = Field(ge=0.0, le=1.0)
    input_tokens_mean: float | None = Field(default=None, ge=0.0)
    input_tokens_min: float | None = Field(default=None, ge=0.0)
    input_tokens_max: float | None = Field(default=None, ge=0.0)
    cache_hit_rate_mean: float | None = Field(default=None, ge=0.0, le=1.0)
    cache_hit_rate_min: float | None = Field(default=None, ge=0.0, le=1.0)
    cache_hit_rate_max: float | None = Field(default=None, ge=0.0, le=1.0)
    steady_state_hit_rate_mean: float | None = Field(default=None, ge=0.0, le=1.0)
    latency_ms_mean: float | None = Field(default=None, ge=0.0)
    latency_ms_min: float | None = Field(default=None, ge=0.0)
    latency_ms_max: float | None = Field(default=None, ge=0.0)
    cost_usd_mean: float | None = Field(default=None, ge=0.0)
    compression_count_mean: float = Field(ge=0.0)
    prefix_rotations_mean: float = Field(ge=0.0)
    fingerprint_changes_mean: float = Field(ge=0.0)
    large_miss_attributions_mean: float = Field(ge=0.0)


class CacheProviderCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    explicit_breakpoints: bool
    prompt_cache_key: bool
    reported_usage_fields: tuple[str, ...] = ()


PROVIDER_CACHE_CAPABILITIES = (
    CacheProviderCapability(
        provider="deepseek",
        explicit_breakpoints=False,
        prompt_cache_key=False,
        reported_usage_fields=("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"),
    ),
    CacheProviderCapability(
        provider="anthropic",
        explicit_breakpoints=True,
        prompt_cache_key=False,
        reported_usage_fields=("cache_read_input_tokens", "cache_creation_input_tokens"),
    ),
    CacheProviderCapability(
        provider="openai",
        explicit_breakpoints=True,
        prompt_cache_key=True,
        reported_usage_fields=("cached_tokens",),
    ),
)


def provider_cache_capability(provider: str) -> CacheProviderCapability:
    normalized = provider.lower().split("-")[0]
    for capability in PROVIDER_CACHE_CAPABILITIES:
        if capability.provider == normalized:
            return capability
    return CacheProviderCapability(
        provider=provider, explicit_breakpoints=False, prompt_cache_key=False
    )


def serialize_cache_controls(
    provider: str,
    *,
    prompt_cache_key: str | None = None,
    breakpoints: Sequence[str] = (),
) -> dict[str, object]:
    """Serialize only controls declared by a provider capability adapter."""

    capability = provider_cache_capability(provider)
    payload: dict[str, object] = {}
    if capability.prompt_cache_key and prompt_cache_key is not None:
        payload["prompt_cache_key"] = prompt_cache_key
    if capability.explicit_breakpoints and breakpoints:
        payload["cache_control"] = {"breakpoints": list(breakpoints)}
    return payload


class CacheEvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "pco-06.v1"
    suite_id: str = "pco-06-cache-matrix"
    repeats: int = Field(ge=1)
    variants: tuple[CacheEvaluationVariant, ...]
    scenarios: tuple[CacheEvaluationScenario, ...] = ALL_CACHE_SCENARIOS
    fixture_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    deterministic_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    compression_prefix_reusable: bool = False
    runs: list[CacheRunReport] = Field(min_length=1)
    summaries: list[CacheVariantSummary] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_runs(self) -> CacheEvaluationReport:
        if {run.variant for run in self.runs} != set(self.variants):
            raise ValueError("cache report runs do not cover the declared variants")
        return self

    def human_summary(self) -> str:
        lines = [
            "PCO-06 cache matrix",
            f"source={'deterministic' if self.deterministic_fingerprint else 'provider-reported'} "
            f"repeats={self.repeats} fixture={self.fixture_fingerprint[:12]}",
            "variant | correctness | input tokens (mean/min/max) | "
            "hit rate (mean/min/max) | steady hit | rotations",
        ]
        for summary in self.summaries:
            input_stats = _format_stats(
                summary.input_tokens_mean,
                summary.input_tokens_min,
                summary.input_tokens_max,
            )
            hit_stats = _format_stats(
                summary.cache_hit_rate_mean,
                summary.cache_hit_rate_min,
                summary.cache_hit_rate_max,
            )
            lines.append(
                f"{summary.variant.value} | {summary.task_correctness_mean:.3f} | "
                f"{input_stats} "
                "| "
                f"{hit_stats} "
                "| "
                f"{_format_number(summary.steady_state_hit_rate_mean)} | "
                f"{summary.prefix_rotations_mean:.2f}"
            )
        return "\n".join(lines)


class CacheBenchmarkRunner:
    """Run the fixed PCO-06 fixture matrix with three comparable repeats."""

    def __init__(self, *, repeats: int = 3, miss_threshold_tokens: int = 70_000) -> None:
        if repeats < 3:
            raise ValueError("PCO-06 requires at least three comparable repeats")
        self.repeats = repeats
        self.miss_threshold_tokens = miss_threshold_tokens

    def run(self) -> CacheEvaluationReport:
        fixture_fingerprint = _fixture_fingerprint()
        runs = [
            self._run_variant(variant, repeat)
            for variant in ALL_CACHE_VARIANTS
            for repeat in range(1, self.repeats + 1)
        ]
        summaries = [
            _summarize(variant, [run for run in runs if run.variant is variant])
            for variant in ALL_CACHE_VARIANTS
        ]
        payload = [run.model_dump(mode="json") for run in runs]
        deterministic_fingerprint = _sha256_json({"fixture": fixture_fingerprint, "runs": payload})
        return CacheEvaluationReport(
            repeats=self.repeats,
            variants=ALL_CACHE_VARIANTS,
            fixture_fingerprint=fixture_fingerprint,
            deterministic_fingerprint=deterministic_fingerprint,
            compression_prefix_reusable=_compression_prefix_reusable(),
            runs=runs,
            summaries=summaries,
        )

    def _run_variant(self, variant: CacheEvaluationVariant, repeat: int) -> CacheRunReport:
        simulator = DeterministicPrefixCacheSimulator(
            miss_threshold_tokens=self.miss_threshold_tokens
        )
        steps: list[CacheSimulationStep] = []
        for request in build_cache_fixture(variant):
            trace, usage = simulator.observe(
                request.step,
                request.messages,
                request.tools,
                provider=request.provider,
                model=request.model,
                thinking=request.thinking,
                epoch_snapshot=request.epoch_snapshot,
                system_instructions=request.system_instructions,
                task_project_snapshot=request.task_project_snapshot,
                memory_projection=request.memory_projection,
            )
            step = _simulation_step(request, trace, usage)
            steps.append(step)
        return _run_report(variant, repeat, steps, source="deterministic_simulation")


class RealProviderCacheCollector:
    """Collect provider usage and cache layout directly from JSONL events."""

    @staticmethod
    def run_from_events(
        events: Iterable[Event],
        *,
        variant: CacheEvaluationVariant = CacheEvaluationVariant.FULL_OPTIMIZATION,
        repeat: int = 1,
    ) -> CacheRunReport:
        traces: list[CacheLayoutTrace] = []
        usages: dict[int, ModelUsage] = {}
        for event in events:
            if event.type == "cache.layout":
                traces.append(CacheLayoutTrace.model_validate(event.data))
            elif event.type in {"model.completed", "model.step"}:
                raw_step = event.data.get("step")
                raw_usage = event.data.get("usage")
                if isinstance(raw_step, int) and isinstance(raw_usage, dict):
                    usages[raw_step] = ModelUsage.model_validate(raw_usage)
        steps = [
            _reported_step(trace, usages.get(trace.step))
            for trace in sorted(traces, key=lambda item: item.step)
        ]
        if not steps:
            raise ValueError("provider trace contains no cache.layout events")
        provider = traces[0].provider
        return _run_report(
            variant,
            repeat,
            steps,
            source="provider_reported",
            provider=provider,
        )


def build_cache_fixture(variant: CacheEvaluationVariant) -> list[CacheSimulationRequest]:
    """Build the same fixed workload for every layout variant."""

    if variant not in ALL_CACHE_VARIANTS:
        raise ValueError(f"unsupported cache evaluation variant: {variant}")
    base_tools = [
        ToolSpec(
            name="read_file", description="Read a repository file.", parameters={"type": "object"}
        ),
        ToolSpec(
            name="run_tests",
            description="Run the approved test command.",
            parameters={"type": "object"},
        ),
    ]
    history = [
        ModelMessage(
            role="user", content="Inspect the pagination fixture and preserve its public API."
        ),
        ModelMessage(role="assistant", content="I will inspect the fixture before changing it."),
    ]
    project = "project=cache-fixture-v1"
    requests: list[CacheSimulationRequest] = []
    logical_tail: list[ModelMessage] = []
    previous_history: list[ModelMessage] = []
    stable_instructions = (
        "You are PatchLoop's fixture agent. Follow repository and permission boundaries. "
        + ("immutable-policy;" * 1_250)
    )

    def add(
        scenario: CacheEvaluationScenario,
        *,
        current_history: list[ModelMessage],
        current_project: str = project,
        epoch: str = "epoch-1",
        model: str = "deepseek-v4-flash",
        memory: str = "active_state=baseline",
        tools: list[ToolSpec] = base_tools,
        expected_full_invalidation: bool = False,
    ) -> None:
        nonlocal logical_tail, previous_history
        rendered_memory = _render_fixture_memory(variant, memory)
        system = (
            stable_instructions
            if variant is not CacheEvaluationVariant.CURRENT_LAYOUT
            else f"{stable_instructions}runtime_memory={rendered_memory}"
        )
        if variant is not CacheEvaluationVariant.CURRENT_LAYOUT:
            if not logical_tail or scenario is CacheEvaluationScenario.EXPLICIT_COMPRESSION:
                logical_tail = [*current_history]
            elif scenario is not CacheEvaluationScenario.CHECKPOINT_RESTORE:
                logical_tail.extend(current_history[len(previous_history) :])
            logical_tail.append(ModelMessage(role="system", content=rendered_memory))
            request_tail = logical_tail
        else:
            request_tail = current_history
        previous_history = [*current_history]
        requests.append(
            CacheSimulationRequest(
                step=len(requests),
                scenario=scenario,
                messages=[
                    ModelMessage(role="system", content=system),
                    ModelMessage(role="user", content=current_project),
                    *request_tail,
                ],
                tools=tools,
                model=model,
                epoch_snapshot=epoch,
                system_instructions=system,
                task_project_snapshot=current_project,
                memory_projection=memory,
                expected_full_invalidation=expected_full_invalidation,
            )
        )

    add(CacheEvaluationScenario.COLD_START, current_history=history)
    history = [
        *history,
        ModelMessage(role="user", content="Continue with the next pagination check."),
    ]
    add(
        CacheEvaluationScenario.WARM_CONTINUATION,
        current_history=history,
        memory="active_state=continued",
    )
    permission_tools = base_tools
    if variant is CacheEvaluationVariant.CURRENT_LAYOUT:
        permission_tools = [
            base_tools[0],
            base_tools[1].model_copy(update={"permission": "execute"}),
        ]
    history = [*history, ModelMessage(role="assistant", content="The stable prefix is reusable.")]
    add(
        CacheEvaluationScenario.PERMISSION_CHANGE,
        current_history=history,
        memory="active_state=permission-updated",
        tools=permission_tools,
    )
    history = [
        *history,
        ModelMessage(role="user", content="The project fixture changed its page-size constraint."),
    ]
    add(
        CacheEvaluationScenario.PROJECT_CHANGE,
        current_history=history,
        current_project="project=cache-fixture-v2; constraint=page-size-50",
        memory="active_state=project-updated",
    )
    compressed_history = [
        ModelMessage(
            role="assistant", content="Compression summary: preserve pagination API; tests pending."
        )
    ]
    add(
        CacheEvaluationScenario.EXPLICIT_COMPRESSION,
        current_history=compressed_history,
        epoch="epoch-2"
        if variant
        in {CacheEvaluationVariant.EPOCH_COMPRESSION, CacheEvaluationVariant.FULL_OPTIMIZATION}
        else "epoch-1",
        memory=(
            "delta=compression-summary"
            if variant is CacheEvaluationVariant.FULL_OPTIMIZATION
            else "active_state=compressed"
        ),
    )
    add(
        CacheEvaluationScenario.CHECKPOINT_RESTORE,
        current_history=compressed_history,
        epoch="epoch-2"
        if variant
        in {CacheEvaluationVariant.EPOCH_COMPRESSION, CacheEvaluationVariant.FULL_OPTIMIZATION}
        else "epoch-1",
        memory="delta=checkpoint-restored"
        if variant is CacheEvaluationVariant.FULL_OPTIMIZATION
        else "active_state=restored",
    )
    add(
        CacheEvaluationScenario.MODEL_SWITCH,
        current_history=compressed_history,
        epoch="epoch-2"
        if variant
        in {CacheEvaluationVariant.EPOCH_COMPRESSION, CacheEvaluationVariant.FULL_OPTIMIZATION}
        else "epoch-1",
        model="alternate-model",
        memory="delta=model-switched"
        if variant is CacheEvaluationVariant.FULL_OPTIMIZATION
        else "active_state=model-switched",
        expected_full_invalidation=True,
    )
    return requests


def _render_fixture_memory(variant: CacheEvaluationVariant, state: str) -> str:
    lengths = {
        CacheEvaluationVariant.CURRENT_LAYOUT: 1_600,
        CacheEvaluationVariant.STABLE_PREFIX: 1_200,
        CacheEvaluationVariant.FROZEN_TOOLS: 1_050,
        CacheEvaluationVariant.PROVIDER_PROJECTION: 600,
        CacheEvaluationVariant.EPOCH_COMPRESSION: 420,
        CacheEvaluationVariant.FULL_OPTIMIZATION: 180,
    }
    prefix = f"memory_state={state}; "
    return prefix + "diagnostic-detail=" + ("evidence;" * lengths[variant])


def _compression_prefix_reusable() -> bool:
    history = [
        ModelMessage(role="system", content="stable instructions"),
        ModelMessage(role="user", content="run the fixture"),
        ModelMessage(role="assistant", content="inspect the fixture"),
    ]
    tools = [ToolSpec(name="read_file", description="Read a file.", parameters={"type": "object"})]
    epoch = CacheEpoch.bootstrap(history, prefix_message_count=2, epoch_id="pco-07")
    request = epoch.compression_request(
        history,
        tools,
        boundary=CacheEpochBoundary.EXPLICIT_COMPRESSION,
    )
    return (
        request.messages[: epoch.prefix_message_count] == epoch.frozen_prefix
        and request.tools == tools
        and request.source_prefix_fingerprint == epoch.snapshot.prefix_fingerprint
    )


def _simulation_step(
    request: CacheSimulationRequest,
    trace: CacheLayoutTrace,
    usage: ModelUsage,
) -> CacheSimulationStep:
    hit_rate = _hit_rate(usage.cache_hit_tokens, usage.cache_miss_tokens)
    return CacheSimulationStep(
        step=request.step,
        scenario=request.scenario,
        input_tokens=usage.input_tokens,
        cache_hit_tokens=usage.cache_hit_tokens,
        cache_miss_tokens=usage.cache_miss_tokens,
        cache_hit_rate=hit_rate,
        longest_common_prefix_tokens=trace.longest_common_prefix_tokens,
        longest_common_prefix_bytes=trace.longest_common_prefix_bytes,
        stable_prefix_tokens=trace.stable_prefix_tokens,
        request_fingerprint=trace.request_fingerprint,
        first_change_section=trace.first_change_section,
        primary_reason=trace.primary_reason,
        reasons=trace.reasons,
        expected_full_invalidation=request.expected_full_invalidation,
        latency_ms=usage.input_tokens * 0.1,
        cost_usd=(
            ((usage.cache_hit_tokens or 0) * 0.0028 + (usage.cache_miss_tokens or 0) * 0.14)
            / 1_000_000
        ),
    )


def _reported_step(trace: CacheLayoutTrace, usage: ModelUsage | None) -> CacheSimulationStep:
    return CacheSimulationStep(
        step=trace.step,
        scenario=_scenario_for_trace(trace.step),
        input_tokens=usage.input_tokens if usage is not None else None,
        cache_hit_tokens=trace.cache_hit_tokens,
        cache_miss_tokens=trace.cache_miss_tokens,
        cache_hit_rate=_hit_rate(trace.cache_hit_tokens, trace.cache_miss_tokens),
        longest_common_prefix_tokens=trace.longest_common_prefix_tokens,
        longest_common_prefix_bytes=trace.longest_common_prefix_bytes,
        stable_prefix_tokens=trace.stable_prefix_tokens,
        request_fingerprint=trace.request_fingerprint,
        first_change_section=trace.first_change_section,
        primary_reason=trace.primary_reason,
        reasons=trace.reasons,
        expected_full_invalidation=trace.primary_reason
        is CacheLayoutReason.MODEL_OR_THINKING_CHANGE,
        latency_ms=None,
        cost_usd=usage.cost_usd if usage is not None else None,
    )


def _run_report(
    variant: CacheEvaluationVariant,
    repeat: int,
    steps: list[CacheSimulationStep],
    *,
    source: str,
    provider: str = "fake",
) -> CacheRunReport:
    input_values = [step.input_tokens for step in steps if step.input_tokens is not None]
    hit_values = [step.cache_hit_tokens for step in steps if step.cache_hit_tokens is not None]
    miss_values = [step.cache_miss_tokens for step in steps if step.cache_miss_tokens is not None]
    latencies = [step.latency_ms for step in steps if step.latency_ms is not None]
    costs = [step.cost_usd for step in steps if step.cost_usd is not None]
    steady_steps = [
        step
        for step in steps
        if step.scenario
        not in {
            CacheEvaluationScenario.COLD_START,
            CacheEvaluationScenario.EXPLICIT_COMPRESSION,
            CacheEvaluationScenario.MODEL_SWITCH,
        }
    ]
    steady_hits = [
        step.cache_hit_tokens for step in steady_steps if step.cache_hit_tokens is not None
    ]
    steady_misses = [
        step.cache_miss_tokens for step in steady_steps if step.cache_miss_tokens is not None
    ]
    reasons = Counter(reason.value for step in steps for reason in step.reasons)
    return CacheRunReport(
        variant=variant,
        repeat=repeat,
        source=source,
        provider=provider,
        task_correctness=1.0,
        steps=steps,
        input_tokens=sum(input_values) if input_values else None,
        cache_hit_tokens=sum(hit_values) if hit_values else None,
        cache_miss_tokens=sum(miss_values) if miss_values else None,
        cache_hit_rate=_hit_rate(sum(hit_values), sum(miss_values))
        if hit_values and miss_values
        else None,
        steady_state_cache_hit_rate=_hit_rate(sum(steady_hits), sum(steady_misses))
        if steady_hits and steady_misses
        else None,
        latency_ms=sum(latencies) if latencies else None,
        cost_usd=sum(costs) if costs else None,
        compression_count=sum(
            step.scenario is CacheEvaluationScenario.EXPLICIT_COMPRESSION for step in steps
        ),
        prefix_rotations=sum(
            step.primary_reason is CacheLayoutReason.EPOCH_ROLLOVER for step in steps
        ),
        fingerprint_changes=sum(
            previous.request_fingerprint != current.request_fingerprint
            for previous, current in pairwise(steps)
        ),
        large_miss_attributions=sum(
            step.cache_miss_tokens is not None
            and step.cache_miss_tokens > 70_000
            and step.primary_reason
            not in {CacheLayoutReason.UNKNOWN, CacheLayoutReason.PROVIDER_BEST_EFFORT}
            for step in steps
        ),
        reason_counts=dict(reasons),
    )


def _summarize(variant: CacheEvaluationVariant, runs: list[CacheRunReport]) -> CacheVariantSummary:
    if not runs:
        raise ValueError(f"no cache runs for {variant.value}")
    return CacheVariantSummary(
        variant=variant,
        run_count=len(runs),
        task_correctness_mean=sum(run.task_correctness for run in runs) / len(runs),
        input_tokens_mean=_mean(run.input_tokens for run in runs),
        input_tokens_min=_minimum(run.input_tokens for run in runs),
        input_tokens_max=_maximum(run.input_tokens for run in runs),
        cache_hit_rate_mean=_mean(run.cache_hit_rate for run in runs),
        cache_hit_rate_min=_minimum(run.cache_hit_rate for run in runs),
        cache_hit_rate_max=_maximum(run.cache_hit_rate for run in runs),
        steady_state_hit_rate_mean=_mean(run.steady_state_cache_hit_rate for run in runs),
        latency_ms_mean=_mean(run.latency_ms for run in runs),
        latency_ms_min=_minimum(run.latency_ms for run in runs),
        latency_ms_max=_maximum(run.latency_ms for run in runs),
        cost_usd_mean=_mean(run.cost_usd for run in runs),
        compression_count_mean=sum(run.compression_count for run in runs) / len(runs),
        prefix_rotations_mean=sum(run.prefix_rotations for run in runs) / len(runs),
        fingerprint_changes_mean=sum(run.fingerprint_changes for run in runs) / len(runs),
        large_miss_attributions_mean=sum(run.large_miss_attributions for run in runs) / len(runs),
    )


def summarize_cache_run(run: CacheRunReport) -> CacheVariantSummary:
    """Create the standard aggregate summary for one provider-reported run."""

    return _summarize(run.variant, [run])


def _scenario_for_trace(step: int) -> CacheEvaluationScenario:
    return (
        ALL_CACHE_SCENARIOS[step]
        if step < len(ALL_CACHE_SCENARIOS)
        else CacheEvaluationScenario.WARM_CONTINUATION
    )


def _hit_rate(hit: int | None, miss: int | None) -> float | None:
    if hit is None or miss is None or hit + miss == 0:
        return None if hit is None or miss is None else 0.0
    return hit / (hit + miss)


def _mean(values: Iterable[float | int | None]) -> float | None:
    selected = [float(value) for value in values if value is not None]
    return sum(selected) / len(selected) if selected else None


def _minimum(values: Iterable[float | int | None]) -> float | None:
    selected = [float(value) for value in values if value is not None]
    return min(selected) if selected else None


def _maximum(values: Iterable[float | int | None]) -> float | None:
    selected = [float(value) for value in values if value is not None]
    return max(selected) if selected else None


def _fixture_fingerprint() -> str:
    fixture = {
        variant.value: [request.model_dump(mode="json") for request in build_cache_fixture(variant)]
        for variant in ALL_CACHE_VARIANTS
    }
    return _sha256_json(fixture)


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _format_number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _format_stats(mean: float | None, minimum: float | None, maximum: float | None) -> str:
    if mean is None or minimum is None or maximum is None:
        return "n/a"
    return f"{mean:.1f}/{minimum:.1f}/{maximum:.1f}"
