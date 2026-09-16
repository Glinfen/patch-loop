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
from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.context import ContextEngine
from patchloop.domain import (
    AppendOnlyOptimizationVersion,
    PromptCacheLayout,
    Task,
    TaskBudget,
    TaskExecutionConfig,
    TaskStatus,
    ToolCall,
)
from patchloop.evaluation.cache_simulator import DeterministicPrefixCacheSimulator
from patchloop.events import Event
from patchloop.prompt_cache import (
    CacheEpoch,
    CacheEpochBoundary,
    CacheLayoutReason,
    CacheLayoutTrace,
)
from patchloop.providers.base import ModelMessage, ModelUsage, ToolSpec


class CacheEvaluationVariant(StrEnum):
    CURRENT_LAYOUT = "current_layout"
    STABLE_PREFIX = "stable_prefix"
    FROZEN_TOOLS = "frozen_tools"
    PROVIDER_PROJECTION = "provider_projection"
    EPOCH_COMPRESSION = "epoch_compression"
    FULL_OPTIMIZATION = "full_optimization"
    APPEND_ONLY = "append_only"


class CacheEvaluationScenario(StrEnum):
    COLD_START = "cold_start"
    WARM_CONTINUATION = "warm_continuation"
    PERMISSION_CHANGE = "permission_change"
    PROJECT_CHANGE = "project_change"
    EXPLICIT_COMPRESSION = "explicit_compression"
    CHECKPOINT_RESTORE = "checkpoint_restore"
    MODEL_SWITCH = "model_switch"


ALL_CACHE_VARIANTS: tuple[CacheEvaluationVariant, ...] = tuple(
    variant
    for variant in CacheEvaluationVariant
    if variant is not CacheEvaluationVariant.APPEND_ONLY
)
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
    request_id: str | None = None
    source_request_id: str | None = None
    comparison_kind: str | None = None
    previous_request_is_prefix: bool | None = None
    tools_unchanged: bool | None = None
    binding_unchanged: bool | None = None
    first_changed_message_index: int | None = None
    metric_basis: str | None = None
    common_prefix_message_count: int | None = None
    common_prefix_estimated_tokens: int | None = None
    output_tokens: int | None = Field(default=None, ge=0)
    usage_complete: bool | None = None
    cost_status: str | None = None
    restored: bool = False


class CacheKnownTotals(BaseModel):
    """Provider values that are known even when a run has an unknown attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: float | None = Field(default=None, ge=0.0)
    cost_usd: float | None = Field(default=None, ge=0.0)


class CachePurposeSummary(BaseModel):
    """Request, attempt, usage and duration totals for one Provider purpose."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_count: int = Field(default=0, ge=0)
    completed_request_count: int = Field(default=0, ge=0)
    attempt_count: int = Field(default=0, ge=0)
    unknown_usage_attempts: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: float | None = Field(default=None, ge=0.0)
    cost_usd: float | None = Field(default=None, ge=0.0)


class CacheRunReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    variant: CacheEvaluationVariant
    repeat: int = Field(ge=1)
    source: str = "deterministic_simulation"
    provider: str = "fake"
    task_correctness: float | None = Field(default=None, ge=0.0, le=1.0)
    steps: list[CacheSimulationStep] = Field(min_length=1)
    input_tokens: int | None = Field(default=None, ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    steady_state_cache_hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    latency_ms: float | None = Field(default=None, ge=0.0)
    cost_usd: float | None = Field(default=None, ge=0.0)
    compression_count: int = Field(default=0, ge=0)
    prefix_rotations: int = Field(default=0, ge=0)
    fingerprint_changes: int = Field(default=0, ge=0)
    large_miss_attributions: int = Field(default=0, ge=0)
    reason_counts: dict[str, int] = Field(default_factory=dict)
    prefix_schema_version: str | None = None
    comparison_counts: dict[str, int] = Field(default_factory=dict)
    restored_request_count: int = Field(default=0, ge=0)
    model: str | None = None
    endpoint_fingerprint: str | None = None
    binding_fingerprint: str | None = None
    input_budget: int | None = None
    pricing_version: str | None = None
    started_at: str | None = None
    batch_id: str | None = None
    task_case: str | None = None
    pair_id: str | None = None
    request_linkage_complete: bool = False
    unknown_usage_attempts: int | None = None
    tool_rounds: int | None = None
    retrieval_state_count: int | None = None
    recovery_verified: bool | None = None
    max_summary_messages: int | None = None
    ordinary_budget_respected: bool | None = None
    purpose_summaries: dict[str, CachePurposeSummary] = Field(default_factory=dict)
    known_totals: CacheKnownTotals | None = None
    compression_request_count: int | None = Field(default=None, ge=0)
    successful_rollover_count: int | None = Field(default=None, ge=0)
    failed_compression_count: int | None = Field(default=None, ge=0)
    optimization_version: str | None = None
    projection_format: str | None = None
    projection_fallback_reason: str | None = None
    new_memory_tokens: int | None = Field(default=None, ge=0)
    working_item_count: int | None = Field(default=None, ge=0)
    opaque_working_blob_count: int | None = Field(default=None, ge=0)
    decision_reason: str | None = None
    mandatory_rebase_tokens: int | None = Field(default=None, ge=0)
    summary_target_tokens: int | None = Field(default=None, ge=0)
    summary_estimated_tokens: int | None = Field(default=None, ge=0)
    freed_input_tokens: int | None = Field(default=None, ge=0)
    headroom_after_rebase: int | None = None
    total_estimated_input_tokens: int | None = Field(default=None, ge=0)
    fixed_action_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    working_update_count: int | None = Field(default=None, ge=0)
    unrelated_working_republication_count: int | None = Field(default=None, ge=0)


class CacheVariantSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    variant: CacheEvaluationVariant
    run_count: int = Field(ge=1)
    task_correctness_mean: float | None = Field(default=None, ge=0.0, le=1.0)
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
    offline_checks: dict[str, bool] = Field(default_factory=dict)
    revision: str | None = None
    source_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_runs(self) -> CacheEvaluationReport:
        if {run.variant for run in self.runs} != set(self.variants):
            raise ValueError("cache report runs do not cover the declared variants")
        return self

    def human_summary(self) -> str:
        lines = [
            (
                "PPS prompt-prefix evaluation"
                if self.schema_version.startswith("pps")
                else "AOP append-only overhead evaluation"
                if self.schema_version.startswith("aop")
                else "PCO-06 cache matrix"
            ),
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
                f"{summary.variant.value} | {_format_number(summary.task_correctness_mean)} | "
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

    def run_runtime_fixture(
        self,
        repository: str | Path,
        *,
        layout: PromptCacheLayout = PromptCacheLayout.LEGACY,
        repeat: int = 1,
        prefix_suite: bool = False,
        restore: bool = False,
        compress: bool = False,
        optimization_version: AppendOnlyOptimizationVersion = "baseline_v1",
        overhead_case: Literal["contract-migration", "long-output"] | None = None,
    ) -> CacheRunReport:
        """Run six real Runtime tool rounds and report the recorded provider requests.

        Cache token values come from the deterministic simulator attached to the
        FakeProvider. The messages and tool definitions themselves are supplied
        by AgentRuntime, MemoryManager, and ToolGateway.
        """

        if repeat < 1:
            raise ValueError("runtime fixture repeat must be positive")

        from dataclasses import replace

        from patchloop.events import EventLogger
        from patchloop.memory.manager import ManagedMemoryRetrieval
        from patchloop.memory.retrieval import RetrievalLayer, RetrievalSelection
        from patchloop.memory.working import (
            WorkingMemoryItemKind,
            WorkingMemoryProviderEntry,
        )
        from patchloop.persistence import RuntimeCheckpoint, SQLiteStore
        from patchloop.providers import FakeProvider, ModelResponse
        from patchloop.runtime import AgentRuntime
        from patchloop.tools.base import Tool, ToolContext, ToolInputModel
        from patchloop.tools.gateway import ToolGateway

        count = 8 if overhead_case is not None else 36 if compress else 6
        observations: list[int] = []

        class FixtureInput(ToolInputModel):
            index: int = Field(ge=0, le=35)

        class FixtureTool(Tool):
            name = "cache_fixture_observe"
            description = "Return a deterministic repository observation for cache evaluation."
            input_model = FixtureInput

            def run(self, arguments: BaseModel, context: ToolContext) -> str:
                del context
                index = FixtureInput.model_validate(arguments).index
                observations.append(index)
                body = f"observation-{index}: "
                if overhead_case == "contract-migration":
                    body += "contract compatibility evidence; " * 90
                elif overhead_case == "long-output" and index == 2:
                    body += "long deterministic tool output; " * 1_200
                elif overhead_case == "long-output":
                    body += "stable long-output observation; " * 45
                elif compress:
                    body += "evidence " * 250
                elif index == 2:
                    body += "long-tool-output;" * 1_200
                else:
                    body += f"stable repository observation {index}"
                return body

        variant = (
            CacheEvaluationVariant.CURRENT_LAYOUT
            if layout is PromptCacheLayout.LEGACY
            else CacheEvaluationVariant.APPEND_ONLY
            if layout is PromptCacheLayout.APPEND_ONLY
            else CacheEvaluationVariant.STABLE_PREFIX
        )
        with TemporaryDirectory(prefix="patchloop-cache-runtime-") as temporary:
            trace = EventLogger(Path(temporary) / "runtime.jsonl")
            responses = [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id=f"fixture-{repeat}-{index}",
                            name="cache_fixture_observe",
                            arguments={"index": index},
                        )
                    ]
                )
                for index in range(count)
            ]
            responses.append(ModelResponse(content="Completed six fixture observations."))

            class FixtureProvider(FakeProvider):
                index = 0

                def complete(
                    self, messages: list[ModelMessage], tools: list[ToolSpec]
                ) -> ModelResponse:
                    if messages[-1].content.startswith("PATCHLOOP_EPOCH_COMPRESSION_V1"):
                        self.requests.append((list(messages), list(tools)))
                        return ModelResponse(
                            content=json.dumps(
                                {
                                    "constraints": ["Inspect observations"],
                                    "paths": [],
                                    "decisions": [],
                                    "failures": [],
                                    "tests": [],
                                    "unfinished": ["Continue observations"],
                                    "next_step": "Read next observation",
                                }
                            )
                        )
                    self.index += 1
                    return super().complete(messages, tools)

            provider = FixtureProvider(
                responses,
                cache_simulator=None
                if prefix_suite
                else DeterministicPrefixCacheSimulator(
                    miss_threshold_tokens=self.miss_threshold_tokens
                ),
            )

            class FixtureRuntime(AgentRuntime):
                def _retrieve_memory(
                    self,
                    task: Task,
                    total_context_tokens: int,
                    retrieval_token_cap: int | None = None,
                    *,
                    projection_mode: Literal["legacy", "structured_v1"] = "legacy",
                ) -> ManagedMemoryRetrieval:
                    retrieved = super()._retrieve_memory(
                        task,
                        total_context_tokens,
                        retrieval_token_cap,
                        projection_mode=projection_mode,
                    )
                    if not prefix_suite or retrieved.context is None:
                        return retrieved
                    if overhead_case is not None:
                        overhead_marker = provider.index
                        files = [
                            WorkingMemoryProviderEntry(
                                key=f"read:src/module_{index}.py",
                                kind=WorkingMemoryItemKind.ACCESSED_FILE,
                                value=(f"src/module_{index}.py compatibility evidence"),
                            )
                            for index in range(3)
                        ]
                        active = WorkingMemoryProviderEntry(
                            key="plan:active",
                            kind=WorkingMemoryItemKind.PLAN,
                            value=f"action-{overhead_marker}",
                            pinned=True,
                        )
                        legacy_blob = "PATCHLOOP_WORKING_MEMORY_V1\n" + json.dumps(
                            [entry.model_dump(mode="json") for entry in [*files, active]],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        selection = RetrievalSelection(
                            id="overhead-working-state",
                            layer=RetrievalLayer.WORKING,
                            text=legacy_blob,
                            provider_text=legacy_blob,
                            provider_items=[*files, active]
                            if projection_mode == "structured_v1"
                            else None,
                            score=1.0,
                            estimated_tokens=max(1, len(legacy_blob) // 4),
                            reason="fixed append-only overhead fixture",
                            diversity_key="overhead-working-state",
                        )
                        return replace(
                            retrieved,
                            context=retrieved.context.model_copy(
                                update={"selections": [selection]},
                            ),
                        )
                    marker = ("A", "B", "A", "B", "C")[min(provider.index, 4)]
                    selection = RetrievalSelection(
                        id="prefix-stimulus",
                        layer=RetrievalLayer.WORKING,
                        text=marker,
                        provider_text=marker,
                        score=1.0,
                        estimated_tokens=1,
                        reason="deterministic fixture",
                        diversity_key="prefix-stimulus",
                    )
                    return replace(
                        retrieved,
                        context=retrieved.context.model_copy(
                            update={"selections": [selection]},
                        ),
                    )

                def _persist_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
                    super()._persist_checkpoint(checkpoint)
                    if restore and checkpoint.next_step_index == 3 and not restored[0]:
                        restored[0] = True
                        raise KeyboardInterrupt("fixture checkpoint restore boundary")

            restored = [False]
            store = SQLiteStore(Path(temporary) / "runtime.db") if restore else None
            root = Path(repository).resolve(strict=True)
            gateway = ToolGateway(ToolContext(root), [FixtureTool()], trace)
            task = Task(
                id=f"cache-runtime-{layout.value.replace('_', '-')}-{repeat}",
                goal="Inspect six deterministic repository observations.",
                repository=str(root),
                budget=TaskBudget(
                    max_steps=count + 2,
                    max_context_tokens=4_500 if compress or overhead_case is not None else 32_000,
                    max_tool_output_chars=3_000 if compress or overhead_case is not None else 8_000,
                ),
                execution=TaskExecutionConfig(
                    prompt_cache_layout=layout,
                    append_only_optimization=optimization_version,
                ),
            )
            runtime = FixtureRuntime(provider, gateway, trace, state_store=store)
            try:
                result = runtime.run(task)
            except KeyboardInterrupt:
                if store is None or not restored[0]:
                    raise
                runtime = FixtureRuntime(
                    provider,
                    ToolGateway(ToolContext(root), [FixtureTool()], trace),
                    trace,
                    state_store=store,
                )
                result = runtime.resume(store.get_task(task.id), store.get_checkpoint(task.id))
            if result.status is not TaskStatus.COMPLETED:
                raise RuntimeError(
                    "Runtime cache fixture failed "
                    f"({result.status.value}, observations={len(observations)}, "
                    f"requests={len(provider.requests)}): {result.error}"
                )
            if result.report is None or result.report.tool_calls != count:
                raise RuntimeError("Runtime cache fixture did not execute six tool rounds")
            if len(provider.requests) < count + 1:
                raise RuntimeError("Runtime cache fixture did not complete six tool rounds")
            recorded_events = trace.read()
            traces = [
                CacheLayoutTrace.model_validate(event.data)
                for event in recorded_events
                if event.type == "cache.layout"
            ]
            if len(traces) != len(provider.requests):
                raise RuntimeError("Runtime cache fixture request trace is incomplete")
            steps = [
                _reported_step(
                    trace_item,
                    None
                    if prefix_suite
                    else ModelUsage(
                        input_tokens=(trace_item.cache_hit_tokens or 0)
                        + (trace_item.cache_miss_tokens or 0)
                    ),
                )
                for trace_item in traces
            ]
        if prefix_suite and restore:
            steps = [item.model_copy(update={"restored": item.step == 3}) for item in steps]
        run = _run_report(variant, repeat, steps, source="deterministic")
        if overhead_case is not None:
            diagnostics = _cache_diagnostics(recorded_events)
            diagnostics["new_memory_tokens"] = sum(
                int(event.data["new_memory_tokens"])
                for event in recorded_events
                if event.type == "cache.compression.decision"
                and isinstance(event.data.get("new_memory_tokens"), int)
            )
            compression_requests = [
                event for event in recorded_events if event.type == "cache.compression.requested"
            ]
            rollovers = [
                event for event in recorded_events if event.type == "cache.epoch.rolled_over"
            ]
            update_count, unrelated_count = _single_item_update_diagnostics()
            action_fingerprint = _sha256_json(
                {
                    "case": overhead_case,
                    "count": count,
                    "goal": task.goal,
                    "budget": task.budget.model_dump(mode="json"),
                    "tool": FixtureTool.name,
                }
            )
            estimated_input = sum(
                ContextEngine.estimate_messages(messages) + ContextEngine.estimate_tools(tools)
                for messages, tools in provider.requests
            )
            run = run.model_copy(
                update={
                    "task_case": overhead_case,
                    "total_estimated_input_tokens": estimated_input,
                    "compression_count": len(compression_requests),
                    "compression_request_count": len(compression_requests),
                    "successful_rollover_count": len(rollovers),
                    "failed_compression_count": len(compression_requests) - len(rollovers),
                    "optimization_version": optimization_version,
                    "fixed_action_fingerprint": action_fingerprint,
                    "working_update_count": update_count,
                    "unrelated_working_republication_count": unrelated_count,
                    **diagnostics,
                }
            )
        if prefix_suite and overhead_case is None:
            from patchloop.prompt_cache.coordinator import compute_prefix_budget
            from patchloop.prompt_cache.epoch import SUMMARY_PREFIX

            if observations != list(range(count)):
                raise RuntimeError("fixture repeated or lost a tool effect")
            run = run.model_copy(
                update={
                    "steps": steps,
                    "task_case": "compression"
                    if compress
                    else "restore"
                    if restore
                    else "ordinary",
                    "tool_rounds": count,
                    "retrieval_state_count": 3,
                    "recovery_verified": restored[0] if restore else None,
                    "input_budget": task.budget.max_context_tokens,
                    "unknown_usage_attempts": 0,
                    "max_summary_messages": max(
                        sum(m.content.startswith(SUMMARY_PREFIX) for m in messages)
                        for messages, _ in provider.requests
                    ),
                    "ordinary_budget_respected": all(
                        ContextEngine.estimate_messages(messages)
                        + ContextEngine.estimate_tools(tools)
                        <= compute_prefix_budget(
                            task.budget, runtime.provider_binding, tools
                        ).ordinary_limit
                        for messages, tools in provider.requests
                        if not messages[-1].content.startswith("PATCHLOOP_EPOCH_COMPRESSION_V1")
                    ),
                }
            )
        return run

    def run_append_only_overhead_suite(self, repository: str | Path) -> CacheEvaluationReport:
        """Compare fixed append-only actions without claiming Provider cache benefit."""

        cases: tuple[Literal["contract-migration", "long-output"], ...] = (
            "contract-migration",
            "long-output",
        )
        versions: tuple[AppendOnlyOptimizationVersion, ...] = (
            "baseline_v1",
            "balanced_v1",
        )
        runs = [
            self.run_runtime_fixture(
                repository,
                layout=PromptCacheLayout.APPEND_ONLY,
                repeat=repeat,
                prefix_suite=True,
                optimization_version=version,
                overhead_case=case,
            )
            for version in versions
            for case in cases
            for repeat in range(1, self.repeats + 1)
        ]
        recovery = self.run_runtime_fixture(
            repository,
            layout=PromptCacheLayout.APPEND_ONLY,
            prefix_suite=True,
            restore=True,
            optimization_version="balanced_v1",
        )
        fingerprint = append_only_overhead_fixture_fingerprint(self.repeats)
        return CacheEvaluationReport(
            schema_version="aop-overhead.v1",
            suite_id="append-only-overhead",
            repeats=self.repeats,
            variants=(CacheEvaluationVariant.APPEND_ONLY,),
            scenarios=(),
            fixture_fingerprint=fingerprint,
            deterministic_fingerprint=_sha256_json([run.model_dump(mode="json") for run in runs]),
            compression_prefix_reusable=all(
                step.previous_request_is_prefix is True
                for run in runs
                for step in run.steps
                if step.comparison_kind in {"ordinary", "compression"}
            ),
            runs=runs,
            summaries=[_summarize(CacheEvaluationVariant.APPEND_ONLY, runs)],
            offline_checks={
                "prefix_invariants": all(
                    step.previous_request_is_prefix is True
                    and step.tools_unchanged is True
                    and step.binding_unchanged is True
                    for run in runs
                    for step in run.steps
                    if step.comparison_kind in {"ordinary", "compression"}
                ),
                "recovery": recovery.recovery_verified is True,
                "single_item_updates": all(
                    run.working_update_count == 100
                    and run.unrelated_working_republication_count == 0
                    for run in runs
                ),
            },
        )

    def run_prefix_suite(self, repository: str | Path) -> CacheEvaluationReport:
        """Record the production Runtime through ordinary, compression and recovery cases."""
        runs = [
            self.run_runtime_fixture(
                repository,
                layout=layout,
                repeat=repeat,
                prefix_suite=True,
                restore=case == "restore",
                compress=case == "compression",
            )
            for layout in (PromptCacheLayout.LEGACY, PromptCacheLayout.APPEND_ONLY)
            for repeat in range(1, self.repeats + 1)
            for case in ("ordinary", "compression", "restore")
        ]
        variants = (CacheEvaluationVariant.CURRENT_LAYOUT, CacheEvaluationVariant.APPEND_ONLY)
        fingerprint = _sha256_json({"suite": "pps-prefix-runtime.v1", "rounds": [6, 36, 6]})
        return CacheEvaluationReport(
            schema_version="pps.v1",
            suite_id="pps-prefix-runtime",
            repeats=self.repeats,
            variants=variants,
            fixture_fingerprint=fingerprint,
            deterministic_fingerprint=_sha256_json([run.model_dump(mode="json") for run in runs]),
            compression_prefix_reusable=all(
                step.previous_request_is_prefix is True
                for run in runs
                if run.variant is CacheEvaluationVariant.APPEND_ONLY
                for step in run.steps
                if step.comparison_kind == "compression"
            ),
            runs=runs,
            summaries=[_summarize(v, [r for r in runs if r.variant is v]) for v in variants],
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
        batch_id: str | None = None,
        task_case: str | None = None,
        pair_id: str | None = None,
        task_id: str | None = None,
    ) -> CacheRunReport:
        selected_by_id: dict[str, Event] = {}
        for event in events:
            if task_id is None or event.task_id == task_id:
                selected_by_id.setdefault(event.id, event)
        selected = list(selected_by_id.values())
        if len({e.task_id for e in selected if e.type == "cache.layout"}) > 1:
            raise ValueError("select one task_id when a trace contains multiple tasks")
        traces: dict[str, CacheLayoutTrace] = {}
        usages: dict[str, ModelUsage] = {}
        legacy_usages: dict[int, ModelUsage] = {}
        requests: dict[str, Event] = {}
        attempts: dict[str, dict[str, Any]] = {}
        rollovers: dict[str, Event] = {}
        diagnostic_events: list[Event] = []
        restored_ids: set[str] = set()
        after_restore = False
        for event in selected:
            request_id = event.data.get("request_id")
            attempt_id = event.data.get("attempt_id")
            if event.type == "task.resumed":
                after_restore = True
            if event.type == "provider.request.started" and isinstance(request_id, str):
                requests.setdefault(request_id, event)
            if event.type == "provider.attempt.started" and isinstance(attempt_id, str):
                attempts.setdefault(
                    attempt_id,
                    {
                        "request_id": request_id if isinstance(request_id, str) else None,
                        "started_at": event.timestamp,
                        "finished_at": None,
                        "usage_unknown": None,
                    },
                )
            if event.type == "provider.attempt.finished" and isinstance(attempt_id, str):
                attempt = attempts.setdefault(
                    attempt_id,
                    {
                        "request_id": request_id if isinstance(request_id, str) else None,
                        "started_at": None,
                        "finished_at": None,
                        "usage_unknown": None,
                    },
                )
                attempt["finished_at"] = event.timestamp
                attempt["usage_unknown"] = event.data.get("usage_unknown") is not False
            if event.type == "cache.epoch.rolled_over" and isinstance(request_id, str):
                rollovers.setdefault(request_id, event)
                diagnostic_events.append(event)
            elif event.type in {"cache.compression.failed", "cache.compression.requested"}:
                diagnostic_events.append(event)
            if event.type == "cache.layout":
                trace = CacheLayoutTrace.model_validate(event.data)
                key = trace.request_id or f"legacy-{len(traces)}"
                traces.setdefault(key, trace)
                if after_restore:
                    restored_ids.add(key)
                    after_restore = False
            elif event.type in {"model.completed", "model.step", "provider.request.completed"}:
                raw_usage = event.data.get("usage")
                if not isinstance(raw_usage, dict):
                    continue
                usage = ModelUsage.model_validate(raw_usage)
                if isinstance(request_id, str):
                    if request_id in usages and usages[request_id] != usage:
                        raise ValueError("conflicting usage for the same Provider request")
                    usages[request_id] = usage
                elif isinstance(event.data.get("step"), int):
                    legacy_usages[event.data["step"]] = usage
                if event.type == "provider.request.completed" and isinstance(attempt_id, str):
                    attempt = attempts.setdefault(
                        attempt_id,
                        {
                            "request_id": request_id if isinstance(request_id, str) else None,
                            "started_at": None,
                            "finished_at": None,
                            "usage_unknown": None,
                        },
                    )
                    attempt["finished_at"] = event.timestamp
                    # A completed request carries its usage. Unknown pricing is
                    # distinct from an attempt whose token usage is unknown.
                    attempt["usage_unknown"] = False
        steps = [
            _reported_step(
                trace, usages.get(key) if trace.request_id else legacy_usages.get(trace.step)
            ).model_copy(update={"restored": key in restored_ids})
            for key, trace in traces.items()
        ]
        if not steps:
            raise ValueError("provider trace contains no cache.layout events")
        run = _run_report(
            variant,
            repeat,
            steps,
            source="deterministic"
            if next(iter(traces.values())).provider == "fake"
            else "provider_reported",
            provider=next(iter(traces.values())).provider,
        )
        metadata = next(iter(requests.values()), None)
        unknown_attempts = sum(
            attempt["usage_unknown"] is not False for attempt in attempts.values()
        )
        # The report includes every started request and every attempt. Missing
        # completion/usage stays unknown; no positional joins or zero-cost guesses.
        linked = bool(requests) and set(requests) == set(traces) == set(usages)
        purpose_by_request = {
            request_id: str(
                event.data.get("purpose")
                or (
                    "epoch_compression"
                    if request_id in traces
                    and traces[request_id].comparison_kind == "compression"
                    else "agent_step"
                    if request_id in traces
                    else "unknown"
                )
            )
            for request_id, event in requests.items()
        }
        purpose_summaries = _purpose_summaries(
            requests, attempts, usages, purpose_by_request
        )
        known_totals = _known_totals(usages, attempts)
        compression_request_ids = {
            request_id
            for request_id in requests
            if purpose_by_request[request_id] == "epoch_compression"
        }
        compression_request_count = len(compression_request_ids)
        successful_rollover_count = len(compression_request_ids & set(rollovers))
        unsuccessful_compressions = compression_request_ids - set(rollovers)
        diagnostics = _cache_diagnostics(diagnostic_events)
        fields: dict[str, Any] = {
            "task_correctness": None,
            "batch_id": batch_id,
            "task_case": task_case,
            "pair_id": pair_id,
            "request_linkage_complete": linked,
            "unknown_usage_attempts": unknown_attempts if attempts else None,
            "cost_usd": run.cost_usd if linked and attempts and unknown_attempts == 0 else None,
            "purpose_summaries": purpose_summaries,
            "known_totals": known_totals,
            "compression_count": compression_request_count if requests else run.compression_count,
            "compression_request_count": compression_request_count if requests else None,
            "successful_rollover_count": successful_rollover_count if requests else None,
            "failed_compression_count": len(unsuccessful_compressions) if requests else None,
            **diagnostics,
        }
        if requests and (not linked or unknown_attempts > 0):
            fields.update(
                {
                    "input_tokens": None,
                    "cache_hit_tokens": None,
                    "cache_miss_tokens": None,
                    "cache_hit_rate": None,
                    "output_tokens": None,
                }
            )
        if metadata is not None:
            fields.update(
                {
                    key: metadata.data.get(key)
                    for key in (
                        "model",
                        "endpoint_fingerprint",
                        "binding_fingerprint",
                        "input_budget",
                        "pricing_version",
                    )
                }
            )
            fields["started_at"] = metadata.timestamp.isoformat()
            for event in requests.values():
                if any(
                    event.data.get(key) != fields[key]
                    for key in (
                        "model",
                        "endpoint_fingerprint",
                        "binding_fingerprint",
                        "input_budget",
                        "pricing_version",
                    )
                ):
                    fields["request_linkage_complete"] = False
        return run.model_copy(update=fields)


def _purpose_summaries(
    requests: dict[str, Event],
    attempts: dict[str, dict[str, Any]],
    usages: dict[str, ModelUsage],
    purpose_by_request: dict[str, str],
) -> dict[str, CachePurposeSummary]:
    purposes = set(purpose_by_request.values())
    summaries: dict[str, CachePurposeSummary] = {}
    for purpose in sorted(purposes):
        request_ids = {
            request_id
            for request_id in requests
            if purpose_by_request[request_id] == purpose
        }
        purpose_attempts = [
            attempt for attempt in attempts.values() if attempt["request_id"] in request_ids
        ]
        purpose_usages = [
            usage for request_id, usage in usages.items() if request_id in request_ids
        ]
        cache_hits = [usage.cache_hit_tokens for usage in purpose_usages]
        cache_misses = [usage.cache_miss_tokens for usage in purpose_usages]
        complete_cost = (
            len(purpose_usages) == len(request_ids)
            and all(attempt["usage_unknown"] is False for attempt in purpose_attempts)
            and all(usage.cost_status != "unknown" for usage in purpose_usages)
        )
        summaries[purpose] = CachePurposeSummary(
            request_count=len(request_ids),
            completed_request_count=len(request_ids & set(usages)),
            attempt_count=len(purpose_attempts),
            unknown_usage_attempts=sum(
                attempt["usage_unknown"] is not False for attempt in purpose_attempts
            ),
            input_tokens=sum(usage.input_tokens for usage in purpose_usages),
            cache_hit_tokens=(
                sum(value for value in cache_hits if value is not None)
                if cache_hits and all(value is not None for value in cache_hits)
                else None
            ),
            cache_miss_tokens=(
                sum(value for value in cache_misses if value is not None)
                if cache_misses and all(value is not None for value in cache_misses)
                else None
            ),
            output_tokens=sum(usage.output_tokens for usage in purpose_usages),
            latency_ms=_attempt_latency_ms(purpose_attempts),
            cost_usd=(
                sum(usage.cost_usd for usage in purpose_usages) if complete_cost else None
            ),
        )
    return summaries


def _known_totals(
    usages: dict[str, ModelUsage],
    attempts: dict[str, dict[str, Any]],
) -> CacheKnownTotals | None:
    if not usages and not attempts:
        return None
    known_usages = list(usages.values())
    cache_hits = [usage.cache_hit_tokens for usage in known_usages]
    cache_misses = [usage.cache_miss_tokens for usage in known_usages]
    known_costs = [usage.cost_usd for usage in known_usages if usage.cost_status != "unknown"]
    return CacheKnownTotals(
        input_tokens=sum(usage.input_tokens for usage in known_usages),
        cache_hit_tokens=(
            sum(value for value in cache_hits if value is not None)
            if cache_hits and all(value is not None for value in cache_hits)
            else None
        ),
        cache_miss_tokens=(
            sum(value for value in cache_misses if value is not None)
            if cache_misses and all(value is not None for value in cache_misses)
            else None
        ),
        output_tokens=sum(usage.output_tokens for usage in known_usages),
        latency_ms=_attempt_latency_ms(list(attempts.values())),
        cost_usd=sum(known_costs) if known_costs else None,
    )


def _attempt_latency_ms(attempts: list[dict[str, Any]]) -> float | None:
    if not attempts:
        return None
    total = 0.0
    for attempt in attempts:
        started_at = attempt["started_at"]
        finished_at = attempt["finished_at"]
        if not isinstance(started_at, datetime) or not isinstance(finished_at, datetime):
            return None
        total += max(0.0, (finished_at - started_at).total_seconds() * 1_000)
    return total


def _cache_diagnostics(events: list[Event]) -> dict[str, Any]:
    fields = (
        "optimization_version",
        "projection_format",
        "projection_fallback_reason",
        "working_item_count",
        "opaque_working_blob_count",
        "decision_reason",
        "mandatory_rebase_tokens",
        "summary_target_tokens",
        "headroom_after_rebase",
    )
    diagnostics: dict[str, Any] = {}
    for field in fields:
        values = [event.data.get(field) for event in events if event.data.get(field) is not None]
        diagnostics[field] = values[-1] if values else None
    for field, event_type in (
        ("new_memory_tokens", "cache.compression.requested"),
        ("summary_estimated_tokens", "cache.epoch.rolled_over"),
        ("freed_input_tokens", "cache.epoch.rolled_over"),
    ):
        values = [
            event.data.get(field)
            for event in events
            if event.type == event_type and isinstance(event.data.get(field), int)
        ]
        diagnostics[field] = sum(values) if values else None
    return diagnostics


def _single_item_update_diagnostics() -> tuple[int, int]:
    """Exercise 100 V2 updates and count unrelated working-entry republications."""

    from patchloop.prompt_cache.publication import MemoryDeltaPublisher

    stable = [
        {
            "type": "working_memory",
            "key": f"read:src/module_{index}.py",
            "field": "read_files",
            "value": f"src/module_{index}.py",
        }
        for index in range(3)
    ]
    changing = {
        "type": "working_memory",
        "key": "plan:active",
        "field": "plan",
        "value": "step-0",
    }
    payload: dict[str, object] = {"working_state": [*stable, changing]}
    first = MemoryDeltaPublisher().preview(
        "overhead-stream",
        payload,
        invalidated_values=[],
        max_message_tokens=2_048,
    )
    publisher = MemoryDeltaPublisher(first.next_state)
    unrelated = 0
    for index in range(1, 101):
        expected_key = "read:src/inserted.py" if index == 50 else "plan:active"
        if index == 50:
            stable = [
                *stable,
                {
                    "type": "working_memory",
                    "key": expected_key,
                    "field": "read_files",
                    "value": "src/inserted.py",
                },
            ]
        else:
            changing = {**changing, "value": f"step-{index}"}
        update = publisher.preview(
            "overhead-stream",
            {"working_state": [*stable, changing]},
            invalidated_values=[],
            max_message_tokens=2_048,
        )
        for message in update.messages:
            envelope = json.loads(message.content.split("\n", 2)[2])
            for operation in envelope.get("operations", []):
                value = operation.get("value")
                if isinstance(value, dict) and value.get("key") != expected_key:
                    unrelated += 1
        publisher = MemoryDeltaPublisher(update.next_state)
    return 100, unrelated


AOP_SOURCE_PATHS: tuple[str, ...] = (
    "benchmarks/run_pps_server.py",
    "providers.toml",
    "src/patchloop/cli.py",
    "src/patchloop/domain.py",
    "src/patchloop/runtime.py",
    "src/patchloop/memory/working.py",
    "src/patchloop/memory/retrieval.py",
    "src/patchloop/prompt_cache/publication.py",
    "src/patchloop/prompt_cache/coordinator.py",
    "src/patchloop/prompt_cache/epoch.py",
    "src/patchloop/evaluation/cache.py",
    "src/patchloop/evaluation/gates.py",
)


def aop_source_fingerprint(repository: str | Path) -> str:
    """Hash the source surface whose drift invalidates AOP L0 readiness."""

    root = Path(repository).resolve(strict=True)
    digest = hashlib.sha256()
    for relative in AOP_SOURCE_PATHS:
        path = root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def append_only_overhead_fixture_fingerprint(repeats: int = 3) -> str:
    """Return the versioned fingerprint for the fixed AOP overhead workload."""

    return _sha256_json(
        {
            "suite": "append-only-overhead.v1",
            "cases": ("contract-migration", "long-output"),
            "versions": ("baseline_v1", "balanced_v1"),
            "repeats": repeats,
            "tool_rounds": 8,
            "single_item_updates": 100,
        }
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
        scenario=(
            CacheEvaluationScenario.EXPLICIT_COMPRESSION
            if trace.comparison_kind == "compression"
            else CacheEvaluationScenario.COLD_START
            if trace.comparison_kind in {"cold_start", "epoch_boundary"}
            else CacheEvaluationScenario.WARM_CONTINUATION
            if trace.comparison_kind == "ordinary"
            else _scenario_for_trace(trace.step)
        ),
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
        cost_usd=usage.cost_usd if usage is not None and usage.cost_status == "estimated" else None,
        request_id=trace.request_id,
        source_request_id=trace.source_request_id,
        comparison_kind=trace.comparison_kind,
        previous_request_is_prefix=trace.previous_request_is_prefix,
        tools_unchanged=trace.tools_unchanged,
        binding_unchanged=trace.binding_unchanged,
        first_changed_message_index=trace.first_changed_message_index,
        metric_basis=trace.metric_basis,
        common_prefix_message_count=trace.common_prefix_message_count,
        common_prefix_estimated_tokens=trace.common_prefix_estimated_tokens,
        output_tokens=usage.output_tokens if usage is not None else None,
        usage_complete=(
            usage is not None
            and usage.input_tokens_reported is not False
            and usage.output_tokens_reported is not False
            and trace.cache_usage_consistent is True
        ),
        cost_status=usage.cost_status if usage is not None else None,
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
        input_tokens=sum(input_values) if len(input_values) == len(steps) else None,
        cache_hit_tokens=sum(hit_values) if len(hit_values) == len(steps) else None,
        cache_miss_tokens=sum(miss_values) if len(miss_values) == len(steps) else None,
        output_tokens=(
            sum(step.output_tokens for step in steps if step.output_tokens is not None)
            if all(step.output_tokens is not None for step in steps)
            else None
        ),
        cache_hit_rate=_hit_rate(sum(hit_values), sum(miss_values))
        if len(hit_values) == len(steps) and len(miss_values) == len(steps)
        else None,
        steady_state_cache_hit_rate=_hit_rate(sum(steady_hits), sum(steady_misses))
        if steady_hits and steady_misses
        else None,
        latency_ms=sum(latencies) if latencies else None,
        cost_usd=sum(costs) if len(costs) == len(steps) else None,
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
        comparison_counts=dict(Counter(step.comparison_kind or "unavailable" for step in steps)),
        restored_request_count=sum(step.restored for step in steps),
        prefix_schema_version="pps.v1" if all(step.metric_basis for step in steps) else None,
    )


def _summarize(variant: CacheEvaluationVariant, runs: list[CacheRunReport]) -> CacheVariantSummary:
    if not runs:
        raise ValueError(f"no cache runs for {variant.value}")
    return CacheVariantSummary(
        variant=variant,
        run_count=len(runs),
        task_correctness_mean=_mean(run.task_correctness for run in runs)
        if all(run.task_correctness is not None for run in runs)
        else None,
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
