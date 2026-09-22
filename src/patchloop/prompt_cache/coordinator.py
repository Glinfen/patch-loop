"""Pure lifecycle coordination for prompt-cache state."""

from __future__ import annotations

import hashlib
import json
import math
import re
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.context.engine import ContextBudgetError, ContextEngine
from patchloop.domain import AppendOnlyOptimizationVersion, PromptCacheLayout, TaskBudget
from patchloop.prompt_cache.diagnostics import (
    CacheDiagnostics,
    CacheDiagnosticsSnapshot,
    CacheLayoutTrace,
)
from patchloop.prompt_cache.epoch import (
    COMPRESSION_INSTRUCTION,
    CacheCompressionRequest,
    CacheEpoch,
    CacheEpochBoundary,
    CacheEpochSnapshot,
    compression_instruction,
    validate_compression_summary,
)
from patchloop.prompt_cache.layout import PromptLayout
from patchloop.prompt_cache.publication import (
    MemoryDeltaPublisher,
    MemoryDeltaTooLarge,
    MemoryPublicationSnapshot,
)
from patchloop.prompt_cache.usage import (
    CacheUsageAccumulator,
    CacheUsageAccumulatorSnapshot,
    CacheUsageCheckpointFields,
    CacheUsageReportFields,
)
from patchloop.providers.base import ModelMessage, ModelUsage, ToolSpec
from patchloop.providers.contracts import ProviderBinding
from patchloop.security import SecretRedactor

_SHA256_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")

# Layouts whose coordinator owns a frozen cache epoch; legacy keeps none.
_FROZEN_EPOCH_LAYOUTS = frozenset({PromptCacheLayout.STABLE, PromptCacheLayout.APPEND_ONLY})


class AppendOnlyOptimizationPolicy(BaseModel):
    """Frozen parameters owned by one append-only optimization version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: AppendOnlyOptimizationVersion
    projection_mode: Literal["legacy", "structured_v1"]
    soft_limit_ratio: float = Field(gt=0.0, le=1.0)
    soft_compression_backoff_steps: int = Field(ge=0)
    summary_target_max_tokens: int | None = Field(default=None, ge=1)
    fixed_projection_budget: bool = False

    @classmethod
    def for_version(cls, version: AppendOnlyOptimizationVersion) -> AppendOnlyOptimizationPolicy:
        if version == "baseline_v1":
            return cls(
                version=version,
                projection_mode="legacy",
                soft_limit_ratio=0.80,
                soft_compression_backoff_steps=0,
            )
        if version == "balanced_v1":
            return cls(
                version=version,
                projection_mode="structured_v1",
                soft_limit_ratio=0.95,
                soft_compression_backoff_steps=3,
                summary_target_max_tokens=1_024,
                fixed_projection_budget=True,
            )
        raise ValueError(f"unsupported append-only optimization version: {version}")


class PromptCacheCoordinatorError(ValueError):
    """Raised when a prompt-cache lifecycle transition is invalid."""


class PromptPrefixViolation(PromptCacheCoordinatorError):
    """Raised when an append-only request changes an already submitted prefix."""

    def __init__(
        self,
        reason: str,
        *,
        message_index: int | None = None,
        expected_fingerprint: str | None = None,
        actual_fingerprint: str | None = None,
    ) -> None:
        self.reason = reason
        self.message_index = message_index
        self.expected_fingerprint = expected_fingerprint
        self.actual_fingerprint = actual_fingerprint
        detail = f"append-only prompt prefix violation: {reason}"
        if message_index is not None:
            detail += f" at message {message_index}"
        if expected_fingerprint is not None and actual_fingerprint is not None:
            detail += f" (expected {expected_fingerprint[:12]}, got {actual_fingerprint[:12]})"
        super().__init__(detail)


class CompressionFailureAction(StrEnum):
    CONTINUE_OLD_EPOCH = "continue_old_epoch"
    PAUSE_CONTEXT_BUDGET = "pause_context_budget"


class PromptCompressionRejected(PromptCacheCoordinatorError):
    """A compression attempt was rejected without committing a new epoch."""

    def __init__(
        self,
        reason: str,
        action: CompressionFailureAction,
        *,
        detail: str | None = None,
    ) -> None:
        self.reason = reason
        self.action = action
        self.detail = detail
        message = f"append-only compression rejected: {reason} ({action.value})"
        if detail is not None:
            message += f": {detail}"
        super().__init__(message)


class PrefixBudget(BaseModel):
    """Token ceilings for append-only requests and their reserved operations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_limit: int = Field(ge=1)
    ordinary_limit: int = Field(ge=1)
    soft_limit: int = Field(ge=1)
    memory_message_limit: int = Field(ge=64)
    summary_limit: int = Field(ge=128)
    compression_instruction: str = Field(default=COMPRESSION_INSTRUCTION, min_length=1)
    summary_target_tokens: int | None = Field(default=None, ge=1)


class CompressionDecision(BaseModel):
    """Pure append-only scheduling result consumed and verified by Runtime/Coordinator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["continue", "compress", "pause"]
    reason: str = Field(min_length=1)
    candidate_input_tokens: int = Field(ge=0)
    mandatory_rebase_tokens: int = Field(ge=0)
    soft_limit: int = Field(ge=1)
    ordinary_limit: int = Field(ge=1)
    summary_target_tokens: int = Field(ge=1)


def compute_prefix_budget(
    task_budget: TaskBudget,
    provider_binding: ProviderBinding | None,
    tools: list[ToolSpec],
    *,
    policy: AppendOnlyOptimizationPolicy | None = None,
) -> PrefixBudget:
    """Calculate the append-only input, compression, summary and memory budgets."""

    tool_tokens = ContextEngine.estimate_tools(tools)
    if provider_binding is None:
        output_tokens = task_budget.max_output_tokens
        context_window = task_budget.max_context_tokens + output_tokens + 256
        for _ in range(8):
            safety_tokens = min(2048, max(256, math.ceil(context_window * 0.01)))
            adjusted_window = task_budget.max_context_tokens + output_tokens + safety_tokens
            if adjusted_window == context_window:
                break
            context_window = adjusted_window
    else:
        output_tokens = provider_binding.generation.max_output_tokens
        context_window = provider_binding.capabilities.context_window_tokens

    safety_tokens = min(2048, max(256, math.ceil(context_window * 0.01)))
    input_limit = min(
        task_budget.max_context_tokens,
        context_window - output_tokens - safety_tokens,
    )
    if input_limit < tool_tokens:
        raise ContextBudgetError(
            f"tool definitions require {tool_tokens} tokens, input budget is {input_limit}"
        )

    active_policy = policy or AppendOnlyOptimizationPolicy.for_version("baseline_v1")
    final_instruction = COMPRESSION_INSTRUCTION
    summary_target: int | None = None
    for _ in range(8):
        compression_message = ModelMessage(role="user", content=final_instruction)
        compression_reserve = ContextEngine.estimate_message(compression_message) + 64
        ordinary_limit = input_limit - compression_reserve
        if ordinary_limit < 1:
            raise ContextBudgetError(
                "input budget cannot reserve enough room for an ordinary request and compression"
            )
        summary_limit = min(2048, max(128, math.floor(ordinary_limit * 0.125)))
        summary_target = (
            min(summary_limit, active_policy.summary_target_max_tokens, output_tokens)
            if active_policy.summary_target_max_tokens is not None
            else None
        )
        next_instruction = compression_instruction(summary_target)
        if next_instruction == final_instruction:
            break
        final_instruction = next_instruction
    else:
        raise ContextBudgetError("compression instruction budget did not converge")

    compression_message = ModelMessage(role="user", content=final_instruction)
    compression_reserve = ContextEngine.estimate_message(compression_message) + 64
    ordinary_limit = input_limit - compression_reserve
    if ordinary_limit < 1:
        raise ContextBudgetError(
            "input budget cannot reserve enough room for an ordinary request and compression"
        )
    memory_message_limit = min(2048, math.floor(ordinary_limit * 0.10))
    if memory_message_limit < 64:
        raise ContextBudgetError(
            "ordinary input budget is too small to reserve the minimum memory message limit"
        )
    soft_limit = math.floor(ordinary_limit * active_policy.soft_limit_ratio)
    summary_limit = min(2048, max(128, math.floor(ordinary_limit * 0.125)))
    summary_target = (
        min(summary_limit, active_policy.summary_target_max_tokens, output_tokens)
        if active_policy.summary_target_max_tokens is not None
        else None
    )
    final_instruction = compression_instruction(summary_target)
    return PrefixBudget(
        input_limit=input_limit,
        ordinary_limit=ordinary_limit,
        soft_limit=soft_limit,
        memory_message_limit=memory_message_limit,
        summary_limit=summary_limit,
        compression_instruction=final_instruction,
        summary_target_tokens=summary_target,
    )


def decide_append_only_compression(
    *,
    policy: AppendOnlyOptimizationPolicy,
    budget: PrefixBudget,
    candidate_input_tokens: int,
    mandatory_rebase_tokens: int,
    step: int,
    last_compression_attempt_step: int | None,
    has_submitted_source: bool,
    recovering_compression: bool = False,
    oversized_delta: bool = False,
) -> CompressionDecision:
    """Choose one bounded action without mutating coordinator or runtime state."""

    values = {
        "candidate_input_tokens": candidate_input_tokens,
        "mandatory_rebase_tokens": mandatory_rebase_tokens,
        "step": step,
    }
    if any(isinstance(value, bool) or value < 0 for value in values.values()):
        raise ValueError("compression decision token counts and step must be non-negative")
    if last_compression_attempt_step is not None and last_compression_attempt_step > step:
        raise ValueError("last compression attempt cannot be after the current step")
    soft_limit = math.floor(budget.ordinary_limit * policy.soft_limit_ratio)
    summary_target = budget.summary_target_tokens or min(
        budget.summary_limit,
        policy.summary_target_max_tokens or budget.summary_limit,
    )

    def decision(
        action: Literal["continue", "compress", "pause"],
        reason: str,
    ) -> CompressionDecision:
        return CompressionDecision(
            action=action,
            reason=reason,
            candidate_input_tokens=candidate_input_tokens,
            mandatory_rebase_tokens=mandatory_rebase_tokens,
            soft_limit=soft_limit,
            ordinary_limit=budget.ordinary_limit,
            summary_target_tokens=summary_target,
        )

    if recovering_compression:
        return decision("compress", "recovering_compression")

    hard_limit_exceeded = candidate_input_tokens > budget.ordinary_limit
    requires_compression = (
        hard_limit_exceeded or oversized_delta or candidate_input_tokens > soft_limit
    )
    if not requires_compression:
        return decision("continue", "below_soft_limit")

    has_rebase_headroom = mandatory_rebase_tokens + budget.summary_limit < budget.ordinary_limit
    if not has_rebase_headroom:
        return decision(
            "pause" if hard_limit_exceeded or oversized_delta else "continue",
            "insufficient_rebase_headroom",
        )
    if not has_submitted_source:
        return decision(
            "pause" if hard_limit_exceeded or oversized_delta else "continue",
            "no_submitted_compression_source",
        )
    if oversized_delta:
        return decision("compress", "oversized_memory_delta")
    if hard_limit_exceeded:
        return decision("compress", "hard_input_limit_exceeded")
    if (
        last_compression_attempt_step is not None
        and step - last_compression_attempt_step < policy.soft_compression_backoff_steps
    ):
        return decision("continue", "soft_compression_backoff")
    return decision("compress", "soft_limit_exceeded")


def estimate_append_only_mandatory_rebase_tokens(
    *,
    root_messages: list[ModelMessage],
    unsent_suffix_messages: list[ModelMessage],
    publication_state: MemoryPublicationSnapshot | None,
    tools: list[ToolSpec],
    memory_message_limit: int,
) -> int:
    """Estimate a summary-free rebuilt epoch using a complete V2 snapshot."""

    rebased = MemoryDeltaPublisher(publication_state).rebase_snapshot(
        "r" * 128,
        max_message_tokens=memory_message_limit,
        source_state=publication_state,
    )
    return ContextEngine.estimate_messages(
        [*root_messages, *unsent_suffix_messages, *rebased.messages]
    ) + ContextEngine.estimate_tools(tools)


def _fingerprint_payload(value: object) -> str:
    encoded = json.dumps(
        _json_compatible(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_compatible(value: object) -> object:
    if isinstance(value, BaseModel):
        return _json_compatible(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_compatible(item) for item in value]
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value


def _message_fingerprint(message: ModelMessage) -> str:
    return _fingerprint_payload(message.model_dump(mode="json"))


def _binding_fingerprint(
    provider: str,
    model: str | None,
    thinking: object | None,
    provider_binding: ProviderBinding | None,
) -> str:
    return _fingerprint_payload(
        {
            "provider": provider,
            "model": model,
            "thinking": thinking,
            "provider_binding": (
                provider_binding.fingerprint if provider_binding is not None else None
            ),
        }
    )


class AppendOnlyPromptState(BaseModel):
    """Recoverable boundary state of the append_only single-transcript contract.

    Message bodies live only in the checkpoint transcript; this state records
    counts and fingerprints so a restored process can verify that the previous
    submitted request is still an exact prefix without a second copy.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    optimization_version: AppendOnlyOptimizationVersion = "baseline_v1"
    last_compression_attempt_step: int | None = Field(default=None, ge=0)
    root_prefix_message_count: int = Field(ge=2)
    last_submitted_message_count: int = Field(default=0, ge=0)
    last_submitted_message_fingerprints: list[str] = Field(default_factory=list)
    last_submitted_epoch_generation: int = Field(default=0, ge=0, le=1_000_000_000)
    last_submitted_tool_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    last_submitted_binding_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    last_submitted_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    epoch_generation: int = Field(default=0, ge=0, le=1_000_000_000)
    compression_source_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    compression_source_message_count: int | None = Field(default=None, ge=0)
    compression_source_epoch_generation: int | None = Field(default=None, ge=0, le=1_000_000_000)
    compression_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    compression_max_output_tokens: int | None = Field(default=None, gt=0)
    compression_instruction: str | None = Field(default=None, min_length=1)
    compression_summary_target_tokens: int | None = Field(default=None, ge=1)
    deferred_compression_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_submission_contract(self) -> Self:
        if self.last_submitted_message_count != len(self.last_submitted_message_fingerprints):
            raise ValueError(
                "append-only submitted message count must equal its fingerprint vector length"
            )
        for fingerprint in self.last_submitted_message_fingerprints:
            if _SHA256_FINGERPRINT.fullmatch(fingerprint) is None:
                raise ValueError("append-only submitted fingerprints must be SHA-256 digests")
        if (
            self.compression_source_message_count is not None
            and self.compression_source_epoch_generation in {None, self.epoch_generation}
            and self.compression_source_message_count > self.last_submitted_message_count
        ):
            raise ValueError("compression source cannot exceed the last submitted message boundary")
        if self.last_submitted_epoch_generation > self.epoch_generation:
            raise ValueError("last submitted epoch generation cannot exceed current generation")
        if (
            self.compression_source_epoch_generation is not None
            and self.compression_source_epoch_generation > self.epoch_generation
        ):
            raise ValueError("compression source generation cannot exceed current generation")
        if (
            self.compression_summary_target_tokens is not None
            and self.compression_instruction is None
        ):
            raise ValueError("compression summary target requires a frozen instruction")
        if self.compression_max_output_tokens is not None and self.compression_request_id is None:
            raise ValueError("compression output limit requires a pending request")
        if self.compression_request_id is not None:
            source = (
                self.compression_source_request_id,
                self.compression_source_message_count,
                self.compression_source_epoch_generation,
            )
            if any(value is None for value in source):
                raise ValueError("pending compression requires complete source metadata")
            if (
                self.compression_source_request_id != self.last_submitted_request_id
                or self.compression_source_message_count != self.last_submitted_message_count
                or self.compression_source_epoch_generation != self.last_submitted_epoch_generation
            ):
                raise ValueError("pending compression source must match the last submitted request")
        return self

    def validate_message_boundaries(self, message_count: int) -> None:
        """Cross-check declared boundaries against the persisted transcript length."""

        if message_count < self.root_prefix_message_count:
            raise ValueError("append-only transcript is shorter than its declared root prefix")
        if message_count < self.last_submitted_message_count:
            raise ValueError("append-only transcript is shorter than its last submitted request")


class PromptCacheCoordinatorSnapshot(BaseModel):
    """Versioned state needed to restore a coordinator without Runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")
    layout: PromptCacheLayout
    cache_epoch_id: str = Field(min_length=1, max_length=128)
    prefix_message_count: int = Field(ge=2)
    frozen_tools: list[ToolSpec] = Field(default_factory=list)
    cache_epoch_state: CacheEpochSnapshot | None = None
    memory_publication_state: MemoryPublicationSnapshot | None = None
    append_only_state: AppendOnlyPromptState | None = None
    cache_diagnostics: CacheDiagnosticsSnapshot
    cache_usage: CacheUsageAccumulatorSnapshot
    miss_threshold_tokens: int = Field(default=70_000, ge=1)
    max_delta_tokens: int = Field(default=2_048, ge=64)

    @model_validator(mode="after")
    def validate_layout_state(self) -> Self:
        if PromptLayout(self.layout).has_frozen_epoch and self.cache_epoch_state is None:
            raise ValueError(
                f"{self.layout.value} prompt-cache coordinator requires an epoch snapshot"
            )
        if self.layout is PromptCacheLayout.LEGACY and self.cache_epoch_state is not None:
            raise ValueError("legacy prompt-cache coordinator cannot carry an epoch snapshot")
        if self.layout is PromptCacheLayout.APPEND_ONLY and self.append_only_state is None:
            raise ValueError("append_only prompt-cache coordinator requires append-only state")
        if self.layout is not PromptCacheLayout.APPEND_ONLY and self.append_only_state is not None:
            raise ValueError("only the append_only layout can carry append-only state")
        return self


class PromptCachePreparedRequest(BaseModel):
    """Provider-independent request data prepared by the coordinator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step: int = Field(ge=0)
    epoch_id: str
    messages: list[ModelMessage]
    tools: list[ToolSpec]
    cache_layout: CacheLayoutTrace


class PromptCacheCompressionPreparation(BaseModel):
    """A compression request whose Provider call is still owned by Runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step: int = Field(ge=0)
    epoch_id: str
    request: CacheCompressionRequest
    cache_layout: CacheLayoutTrace
    source_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_message_count: int | None = Field(default=None, ge=1)
    source_epoch_generation: int | None = Field(default=None, ge=0)
    source_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    unsent_suffix: list[ModelMessage] = Field(default_factory=list)
    candidate_input_tokens: int | None = Field(default=None, ge=0)
    ordinary_limit: int | None = Field(default=None, ge=1)
    summary_limit: int | None = Field(default=None, ge=1)
    compression_instruction: str | None = Field(default=None, min_length=1)
    summary_target_tokens: int | None = Field(default=None, ge=1)
    memory_message_limit: int | None = Field(default=None, ge=1)
    candidate_publication_state: MemoryPublicationSnapshot | None = None
    decision: CompressionDecision | None = None


class PromptCacheCompressionCompletion(BaseModel):
    """Validated append-only epoch and the complete transcript to submit next."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    epoch: CacheEpochSnapshot
    messages: list[ModelMessage]
    publication_state: MemoryPublicationSnapshot
    source_request_id: str
    source_message_count: int = Field(ge=1)
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_input_tokens: int = Field(ge=0)
    rebased_input_tokens: int = Field(ge=0)
    summary_estimated_tokens: int = Field(ge=0)
    summary_target_tokens: int | None = Field(default=None, ge=1)


class PromptCacheResponseObservation(BaseModel):
    """Stable data for Trace/report consumers after a model response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cache_layout: CacheLayoutTrace
    cache_usage: CacheUsageReportFields


class PromptCacheCheckpointFields(CacheUsageCheckpointFields):
    """Legacy RuntimeCheckpoint fields projected from coordinator state."""

    prompt_prefix_message_count: int
    cache_epoch_state: CacheEpochSnapshot | None
    memory_publication_state: MemoryPublicationSnapshot | None
    append_only_state: AppendOnlyPromptState | None
    cache_diagnostics: CacheDiagnosticsSnapshot


class PromptCacheCoordinator:
    """Coordinate pure prompt-cache state without performing external I/O."""

    def __init__(
        self,
        *,
        layout: PromptCacheLayout,
        cache_epoch_id: str,
        prefix_message_count: int,
        frozen_tools: list[ToolSpec],
        cache_epoch: CacheEpoch | None = None,
        publication: MemoryDeltaPublisher | None = None,
        append_only_state: AppendOnlyPromptState | None = None,
        diagnostics: CacheDiagnostics | None = None,
        usage: CacheUsageAccumulator | None = None,
        redactor: SecretRedactor | None = None,
        miss_threshold_tokens: int = 70_000,
        max_delta_tokens: int = 2_048,
    ) -> None:
        if prefix_message_count < 2:
            raise ValueError("prompt-cache coordinator requires at least a system and user prefix")
        if not cache_epoch_id:
            raise ValueError("prompt-cache coordinator epoch id cannot be empty")
        if PromptLayout(layout).has_frozen_epoch and cache_epoch is None:
            raise ValueError(f"{layout.value} prompt-cache coordinator requires an epoch")
        if layout is PromptCacheLayout.LEGACY and cache_epoch is not None:
            raise ValueError("legacy prompt-cache coordinator cannot carry an epoch")
        if layout is PromptCacheLayout.APPEND_ONLY and append_only_state is None:
            raise ValueError("append_only prompt-cache coordinator requires append-only state")
        if (
            layout is PromptCacheLayout.APPEND_ONLY
            and cache_epoch is not None
            and append_only_state is not None
            and cache_epoch.snapshot.root_prefix_message_count is not None
            and cache_epoch.snapshot.root_prefix_message_count
            != append_only_state.root_prefix_message_count
        ):
            raise ValueError("append-only root count must match the epoch root count")
        if (
            layout is PromptCacheLayout.APPEND_ONLY
            and cache_epoch is not None
            and append_only_state is not None
            and append_only_state.epoch_generation != cache_epoch.snapshot.generation
        ):
            raise ValueError("append-only state generation must match the epoch generation")
        if (
            layout is PromptCacheLayout.APPEND_ONLY
            and cache_epoch is not None
            and append_only_state is not None
            and append_only_state.compression_request_id is not None
            and append_only_state.compression_source_epoch_generation
            != cache_epoch.snapshot.generation
        ):
            raise ValueError("pending compression source must belong to the current epoch")
        if cache_epoch is not None and cache_epoch_id != cache_epoch.epoch_id:
            raise ValueError("prompt-cache epoch id must match the restored epoch")
        if cache_epoch is not None and prefix_message_count != cache_epoch.prefix_message_count:
            raise ValueError("prompt-cache prefix count must match the restored epoch")
        if (
            publication is not None
            and publication.snapshot is not None
            and cache_epoch is not None
            and publication.snapshot.epoch_id != cache_epoch.epoch_id
        ):
            raise ValueError("memory publication must belong to the restored epoch")
        if layout is not PromptCacheLayout.APPEND_ONLY and append_only_state is not None:
            raise ValueError("only the append_only layout can carry append-only state")
        self.layout = layout
        self._cache_epoch_id = cache_epoch_id
        self._prefix_message_count = prefix_message_count
        self._frozen_tools = [tool.model_copy(deep=True) for tool in frozen_tools]
        self._epoch = cache_epoch
        self._publication = publication or MemoryDeltaPublisher(max_delta_tokens=max_delta_tokens)
        self._append_only_state = (
            append_only_state.model_copy(deep=True) if append_only_state is not None else None
        )
        self._optimization_policy = (
            AppendOnlyOptimizationPolicy.for_version(self._append_only_state.optimization_version)
            if self._append_only_state is not None
            else None
        )
        self._diagnostics = diagnostics or CacheDiagnostics(
            redactor=redactor,
            miss_threshold_tokens=miss_threshold_tokens,
        )
        self._usage = usage or CacheUsageAccumulator()
        self._miss_threshold_tokens = miss_threshold_tokens
        self._max_delta_tokens = max_delta_tokens
        self._pending_kind: str | None = None
        self._pending_fingerprint: str | None = None
        self._compression_ready_epoch: str | None = None

    @classmethod
    def bootstrap(
        cls,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
        *,
        layout: PromptCacheLayout,
        epoch_id: str = "initial",
        prefix_message_count: int | None = None,
        redactor: SecretRedactor | None = None,
        miss_threshold_tokens: int = 70_000,
        max_delta_tokens: int = 2_048,
        append_only_state: AppendOnlyPromptState | None = None,
        optimization_version: AppendOnlyOptimizationVersion | None = None,
    ) -> PromptCacheCoordinator:
        prefix_count = len(messages) if prefix_message_count is None else prefix_message_count
        selected_optimization = optimization_version or (
            append_only_state.optimization_version
            if append_only_state is not None
            else "baseline_v1"
        )
        if layout is PromptCacheLayout.APPEND_ONLY and append_only_state is None:
            append_only_state = AppendOnlyPromptState(
                root_prefix_message_count=prefix_count,
                optimization_version=selected_optimization,
            )
        if (
            append_only_state is not None
            and append_only_state.optimization_version != selected_optimization
        ):
            raise ValueError("append-only state optimization version does not match bootstrap")
        epoch = (
            CacheEpoch.bootstrap(
                messages,
                prefix_message_count=prefix_count,
                epoch_id=epoch_id,
                redactor=redactor,
                root_prefix_message_count=(
                    append_only_state.root_prefix_message_count
                    if layout is PromptCacheLayout.APPEND_ONLY and append_only_state is not None
                    else None
                ),
            )
            if PromptLayout(layout).has_frozen_epoch
            else None
        )
        return cls(
            layout=layout,
            cache_epoch_id=epoch_id,
            prefix_message_count=prefix_count,
            frozen_tools=PromptLayout.freeze_tools(tools),
            cache_epoch=epoch,
            append_only_state=append_only_state,
            redactor=redactor,
            miss_threshold_tokens=miss_threshold_tokens,
            max_delta_tokens=max_delta_tokens,
        )

    start = bootstrap

    @staticmethod
    def initial_messages(
        system_prompt: str,
        goal: str,
        *,
        layout: PromptCacheLayout,
        project_instructions: str = "",
    ) -> list[ModelMessage]:
        """Build the initial provider message spine for a selected layout."""

        return PromptLayout(layout).initial_messages(
            system_prompt,
            goal,
            project_instructions=project_instructions,
        )

    @classmethod
    def from_legacy_state(
        cls,
        *,
        layout: PromptCacheLayout,
        cache_epoch_id: str,
        prefix_message_count: int,
        frozen_tools: list[ToolSpec],
        messages: list[ModelMessage],
        cache_epoch_state: CacheEpochSnapshot | None = None,
        memory_publication_state: MemoryPublicationSnapshot | None = None,
        append_only_state: AppendOnlyPromptState | None = None,
        cache_diagnostics: CacheDiagnosticsSnapshot | None = None,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_usage_reported_calls: int = 0,
        cache_usage_unreported_calls: int = 0,
        cache_usage_inconsistent_calls: int = 0,
        cache_write_reported_calls: int = 0,
        redactor: SecretRedactor | None = None,
        miss_threshold_tokens: int = 70_000,
        max_delta_tokens: int = 2_048,
    ) -> PromptCacheCoordinator:
        """Restore old RuntimeCheckpoint fields into one coordinator."""

        if append_only_state is not None:
            append_only_state.validate_message_boundaries(len(messages))
        epoch = cache_epoch_state
        if layout is PromptCacheLayout.STABLE and epoch is None:
            epoch = CacheEpoch.bootstrap(
                messages,
                prefix_message_count=prefix_message_count,
                epoch_id=cache_epoch_id,
                redactor=redactor,
            ).snapshot
        diagnostics = CacheDiagnostics(
            redactor=redactor,
            miss_threshold_tokens=miss_threshold_tokens,
        )
        diagnostics.restore(cache_diagnostics)
        return cls(
            layout=layout,
            cache_epoch_id=cache_epoch_id,
            prefix_message_count=prefix_message_count,
            frozen_tools=frozen_tools,
            cache_epoch=(
                CacheEpoch.from_snapshot(
                    epoch,
                    redactor=redactor,
                    root_prefix_message_count=(
                        append_only_state.root_prefix_message_count
                        if layout is PromptCacheLayout.APPEND_ONLY and append_only_state is not None
                        else None
                    ),
                )
                if epoch is not None
                else None
            ),
            publication=(
                MemoryDeltaPublisher(
                    memory_publication_state,
                    max_delta_tokens=max_delta_tokens,
                )
                if layout in _FROZEN_EPOCH_LAYOUTS
                else None
            ),
            append_only_state=append_only_state,
            diagnostics=diagnostics,
            usage=CacheUsageAccumulator.from_legacy(
                cache_hit_tokens=cache_hit_tokens,
                cache_miss_tokens=cache_miss_tokens,
                cache_write_tokens=cache_write_tokens,
                cache_usage_reported_calls=cache_usage_reported_calls,
                cache_usage_unreported_calls=cache_usage_unreported_calls,
                cache_usage_inconsistent_calls=cache_usage_inconsistent_calls,
                cache_write_reported_calls=cache_write_reported_calls,
            ),
            redactor=redactor,
            miss_threshold_tokens=miss_threshold_tokens,
            max_delta_tokens=max_delta_tokens,
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: PromptCacheCoordinatorSnapshot,
        *,
        redactor: SecretRedactor | None = None,
    ) -> PromptCacheCoordinator:
        diagnostics = CacheDiagnostics(
            redactor=redactor,
            miss_threshold_tokens=snapshot.miss_threshold_tokens,
        )
        diagnostics.restore(snapshot.cache_diagnostics)
        return cls(
            layout=snapshot.layout,
            cache_epoch_id=snapshot.cache_epoch_id,
            prefix_message_count=snapshot.prefix_message_count,
            frozen_tools=snapshot.frozen_tools,
            cache_epoch=(
                CacheEpoch.from_snapshot(
                    snapshot.cache_epoch_state,
                    redactor=redactor,
                    root_prefix_message_count=(
                        snapshot.append_only_state.root_prefix_message_count
                        if snapshot.layout is PromptCacheLayout.APPEND_ONLY
                        and snapshot.append_only_state is not None
                        else None
                    ),
                )
                if snapshot.cache_epoch_state is not None
                else None
            ),
            publication=MemoryDeltaPublisher(
                snapshot.memory_publication_state,
                max_delta_tokens=snapshot.max_delta_tokens,
            ),
            append_only_state=snapshot.append_only_state,
            diagnostics=diagnostics,
            usage=CacheUsageAccumulator.from_snapshot(snapshot.cache_usage),
            redactor=redactor,
            miss_threshold_tokens=snapshot.miss_threshold_tokens,
            max_delta_tokens=snapshot.max_delta_tokens,
        )

    @property
    def epoch_id(self) -> str:
        return self._epoch.epoch_id if self._epoch is not None else self._cache_epoch_id

    @property
    def prefix_message_count(self) -> int:
        return (
            self._epoch.prefix_message_count
            if self._epoch is not None
            else self._prefix_message_count
        )

    @property
    def frozen_tools(self) -> list[ToolSpec]:
        return [tool.model_copy(deep=True) for tool in self._frozen_tools]

    @property
    def frozen_prefix(self) -> list[ModelMessage]:
        if self._epoch is None:
            return []
        return self._epoch.frozen_prefix

    @property
    def publication_messages(self) -> list[ModelMessage]:
        return self._publication.messages

    @property
    def publication_snapshot(self) -> MemoryPublicationSnapshot | None:
        return self._publication.snapshot

    @property
    def append_only_state(self) -> AppendOnlyPromptState | None:
        return self._append_only_state

    @property
    def optimization_policy(self) -> AppendOnlyOptimizationPolicy | None:
        return (
            self._optimization_policy.model_copy(deep=True)
            if self._optimization_policy is not None
            else None
        )

    @property
    def usage(self) -> CacheUsageAccumulator:
        return self._usage

    def report_fields(self) -> CacheUsageReportFields:
        return self._usage.report_fields()

    def checkpoint_fields(self) -> PromptCacheCheckpointFields:
        fields: PromptCacheCheckpointFields = {
            "prompt_prefix_message_count": self.prefix_message_count,
            "cache_epoch_state": self._epoch.snapshot if self._epoch is not None else None,
            "memory_publication_state": self._publication.snapshot,
            "append_only_state": self._append_only_state,
            "cache_diagnostics": self._diagnostics.snapshot(),
            **self._usage.checkpoint_fields(),
        }
        return fields

    def materialize_messages(
        self,
        messages: list[ModelMessage],
        *,
        memory_projection: str | None = None,
    ) -> list[ModelMessage]:
        """Materialize an epoch and publish memory without observing a request."""

        request_messages = [message.model_copy(deep=True) for message in messages]
        if self.layout is PromptCacheLayout.APPEND_ONLY:
            return request_messages
        if self._epoch is None:
            return request_messages
        request_messages = self._epoch.materialize(request_messages)
        if memory_projection is not None:
            self._publication, _ = self._publication.publish(
                self.epoch_id,
                memory_projection,
            )
        publication_messages = self._publication.messages
        if not publication_messages:
            return request_messages
        tail = request_messages[self.prefix_message_count :]
        if tail[: len(publication_messages)] == publication_messages:
            return request_messages
        return [
            *request_messages[: self.prefix_message_count],
            *publication_messages,
            *tail,
        ]

    def prepare_request(
        self,
        step: int,
        messages: list[ModelMessage],
        *,
        provider: str,
        model: str | None = None,
        thinking: object | None = None,
        tools: list[ToolSpec] | None = None,
        provider_binding: ProviderBinding | None = None,
        request_id: str | None = None,
        system_instructions: str | None = None,
        task_project_snapshot: object | None = None,
        memory_projection: str | None = None,
    ) -> PromptCachePreparedRequest:
        self._ensure_step_available(step)
        request_tools = (
            [tool.model_copy(deep=True) for tool in tools]
            if tools is not None
            else self.frozen_tools
        )
        if self.layout is PromptCacheLayout.APPEND_ONLY:
            request_messages = [message.model_copy(deep=True) for message in messages]
            self._validate_append_only_request(
                request_messages,
                request_tools,
                provider=provider,
                model=model,
                thinking=thinking,
                provider_binding=provider_binding,
            )
            # V2 publications are transcript messages supplied by the caller. The
            # coordinator must never move an old publication in front of history.
            published_memory = None
        else:
            request_messages = self.materialize_messages(
                messages,
                memory_projection=memory_projection,
            )
            published_memory = memory_projection
            if self._publication.snapshot is not None:
                published_memory = self._publication.rendered
        cache_layout = self._diagnostics.observe(
            step,
            request_messages,
            request_tools,
            provider=provider,
            model=model,
            thinking=thinking,
            epoch_snapshot=(
                self._epoch.diagnostic_snapshot() if self._epoch is not None else self.epoch_id
            ),
            system_instructions=system_instructions,
            task_project_snapshot=task_project_snapshot,
            memory_projection=published_memory,
            request_id=request_id,
            binding_fingerprint=(provider_binding.fingerprint if provider_binding else None),
        )
        prepared = PromptCachePreparedRequest(
            step=step,
            epoch_id=self.epoch_id,
            messages=request_messages,
            tools=request_tools,
            cache_layout=cache_layout,
        )
        if self.layout is PromptCacheLayout.APPEND_ONLY:
            self._record_append_only_submission(
                request_messages,
                request_tools,
                provider=provider,
                model=model,
                thinking=thinking,
                provider_binding=provider_binding,
                request_id=request_id,
            )
        self._mark_pending("request", cache_layout.request_fingerprint)
        return prepared

    def observe_response(
        self,
        prepared: PromptCachePreparedRequest,
        usage: ModelUsage,
        *,
        account_usage: bool = True,
    ) -> PromptCacheResponseObservation:
        self._ensure_pending("request", prepared.cache_layout.request_fingerprint)
        cache_layout = self._diagnostics.finalize(prepared.cache_layout, usage)
        if account_usage:
            self._usage.record(usage)
        self._clear_pending()
        return PromptCacheResponseObservation(
            cache_layout=cache_layout,
            cache_usage=self._usage.report_fields(),
        )

    def prepare_compression(
        self,
        step: int,
        messages: list[ModelMessage],
        *,
        boundary: CacheEpochBoundary,
        provider: str,
        model: str | None = None,
        thinking: object | None = None,
        system_instructions: str | None = None,
        task_project_snapshot: object | None = None,
        source_messages: list[ModelMessage] | None = None,
        unsent_suffix_messages: list[ModelMessage] | None = None,
        candidate_memory_messages: list[ModelMessage] | None = None,
        source_request_id: str | None = None,
        source_message_count: int | None = None,
        budget: PrefixBudget | None = None,
        decision: CompressionDecision | None = None,
        provider_binding: ProviderBinding | None = None,
        candidate_publication_state: MemoryPublicationSnapshot | None = None,
    ) -> PromptCacheCompressionPreparation:
        self._ensure_step_available(step)
        if self._epoch is None:
            raise PromptCacheCoordinatorError(
                "cannot prepare epoch compression when stable layout is disabled"
            )
        if self.layout is PromptCacheLayout.APPEND_ONLY:
            if decision is None:
                raise ValueError("append-only compression requires a verified decision")
            return self._prepare_append_only_compression(
                step,
                messages,
                boundary=boundary,
                provider=provider,
                model=model,
                thinking=thinking,
                system_instructions=system_instructions,
                task_project_snapshot=task_project_snapshot,
                source_messages=source_messages,
                unsent_suffix_messages=unsent_suffix_messages,
                candidate_memory_messages=candidate_memory_messages or [],
                source_request_id=source_request_id,
                source_message_count=source_message_count,
                budget=budget,
                decision=decision,
                provider_binding=provider_binding,
                candidate_publication_state=candidate_publication_state,
            )
        request = self._epoch.compression_request(
            messages,
            self._frozen_tools,
            boundary=boundary,
        )
        cache_layout = self._diagnostics.observe(
            step,
            request.messages,
            request.tools,
            request_kind="compression",
            binding_fingerprint=(provider_binding.fingerprint if provider_binding else None),
            provider=provider,
            model=model,
            thinking=thinking,
            epoch_snapshot=self._epoch.diagnostic_snapshot(),
            system_instructions=system_instructions,
            task_project_snapshot=task_project_snapshot,
        )
        prepared = PromptCacheCompressionPreparation(
            step=step,
            epoch_id=self.epoch_id,
            request=request,
            cache_layout=cache_layout,
        )
        self._mark_pending("compression", cache_layout.request_fingerprint)
        return prepared

    def _prepare_append_only_compression(
        self,
        step: int,
        candidate_messages: list[ModelMessage],
        *,
        boundary: CacheEpochBoundary,
        provider: str,
        model: str | None,
        thinking: object | None,
        system_instructions: str | None,
        task_project_snapshot: object | None,
        source_messages: list[ModelMessage] | None,
        unsent_suffix_messages: list[ModelMessage] | None,
        candidate_memory_messages: list[ModelMessage],
        source_request_id: str | None,
        source_message_count: int | None,
        budget: PrefixBudget | None,
        decision: CompressionDecision,
        provider_binding: ProviderBinding | None,
        candidate_publication_state: MemoryPublicationSnapshot | None,
    ) -> PromptCacheCompressionPreparation:
        state = self._append_only_state
        if state is None or self._epoch is None:
            raise PromptCacheCoordinatorError("append-only compression state is unavailable")
        if not source_request_id:
            raise ValueError("append-only compression requires a source request id")
        if source_messages is None or unsent_suffix_messages is None or budget is None:
            raise ValueError(
                "append-only compression requires an explicit source, unsent suffix and budget"
            )
        if source_message_count != len(source_messages):
            raise PromptPrefixViolation("compression source count does not match its messages")
        if state.last_submitted_epoch_generation != self._epoch.snapshot.generation:
            raise PromptPrefixViolation("compression source belongs to a different epoch")
        if source_message_count != state.last_submitted_message_count:
            raise PromptPrefixViolation("compression source is not the last submitted boundary")
        if state.last_submitted_request_id is not None and (
            source_request_id != state.last_submitted_request_id
        ):
            raise PromptPrefixViolation("compression source request id changed")
        self._validate_append_only_request(
            source_messages,
            self._frozen_tools,
            provider=provider,
            model=model,
            thinking=thinking,
            provider_binding=provider_binding,
        )
        expected_candidate = [
            *source_messages,
            *unsent_suffix_messages,
            *candidate_memory_messages,
        ]
        if expected_candidate != candidate_messages:
            raise PromptPrefixViolation(
                "source, unsent suffix and memory candidate do not form the candidate transcript"
            )
        if not source_messages:
            raise ContextBudgetError("there is no submitted request available to compress")
        estimated_candidate = ContextEngine.estimate_messages(
            candidate_messages
        ) + ContextEngine.estimate_tools(self._frozen_tools)
        validation_budget = max(1, estimated_candidate)
        ContextEngine(
            max_tokens=256,
            max_tool_output_chars=128,
            recent_steps=1,
        ).build_append_only(
            candidate_messages,
            self._frozen_tools,
            max_input_tokens=validation_budget,
        )
        publication_source = candidate_publication_state or self._publication.snapshot
        mandatory_rebase_tokens = estimate_append_only_mandatory_rebase_tokens(
            root_messages=self.frozen_prefix[: state.root_prefix_message_count],
            unsent_suffix_messages=unsent_suffix_messages,
            publication_state=publication_source,
            tools=self._frozen_tools,
            memory_message_limit=budget.memory_message_limit,
        )
        expected_decision = decide_append_only_compression(
            policy=self._optimization_policy
            or AppendOnlyOptimizationPolicy.for_version(state.optimization_version),
            budget=budget,
            candidate_input_tokens=estimated_candidate,
            mandatory_rebase_tokens=mandatory_rebase_tokens,
            step=step,
            last_compression_attempt_step=state.last_compression_attempt_step,
            has_submitted_source=bool(source_messages and source_request_id),
            recovering_compression=state.compression_request_id is not None,
            oversized_delta=boundary is CacheEpochBoundary.EXPLICIT_COMPRESSION,
        )
        if decision != expected_decision or decision.action != "compress":
            raise PromptCacheCoordinatorError(
                "append-only compression decision does not match the candidate"
            )

        source_fingerprint = _fingerprint_payload(
            {
                "epoch_id": self.epoch_id,
                "messages": [message.model_dump(mode="json") for message in source_messages],
                "tools": [tool.model_dump(mode="json") for tool in self._frozen_tools],
                "binding": state.last_submitted_binding_fingerprint,
            }
        )
        if state.deferred_compression_fingerprint == source_fingerprint:
            action = (
                CompressionFailureAction.CONTINUE_OLD_EPOCH
                if estimated_candidate <= budget.ordinary_limit
                else CompressionFailureAction.PAUSE_CONTEXT_BUDGET
            )
            raise PromptCompressionRejected("same_source_deferred", action)

        recovering_compression = state.compression_request_id is not None
        final_instruction = budget.compression_instruction
        if recovering_compression:
            final_instruction = state.compression_instruction or COMPRESSION_INSTRUCTION
        summary_target_tokens = (
            state.compression_summary_target_tokens
            if recovering_compression
            else budget.summary_target_tokens
        )
        request = self._epoch.compression_request(
            source_messages,
            self._frozen_tools,
            boundary=boundary,
            append_only_source=True,
            source_request_id=source_request_id,
            source_message_count=source_message_count,
            instruction=final_instruction,
        )
        ContextEngine(
            max_tokens=256,
            max_tool_output_chars=128,
            recent_steps=1,
        ).build_append_only(
            request.messages,
            request.tools,
            max_input_tokens=budget.input_limit,
        )
        if candidate_publication_state is not None and (
            candidate_publication_state.schema_version != "2.0"
            or candidate_publication_state.epoch_id != self.epoch_id
        ):
            raise ValueError("append-only compression publication candidate must match its epoch")
        if candidate_memory_messages and (
            candidate_publication_state is None
            or candidate_publication_state.messages[-len(candidate_memory_messages) :]
            != candidate_memory_messages
        ):
            raise ValueError("memory candidate messages must match their publication state")
        summary_limit = budget.summary_limit
        if provider_binding is not None:
            summary_limit = min(summary_limit, provider_binding.generation.max_output_tokens)

        next_state = AppendOnlyPromptState.model_validate(
            {
                **state.model_dump(mode="python"),
                "compression_source_request_id": source_request_id,
                "compression_source_message_count": source_message_count,
                "compression_source_epoch_generation": self._epoch.snapshot.generation,
                "compression_request_id": None,
                "compression_max_output_tokens": None,
                "compression_instruction": final_instruction,
                "compression_summary_target_tokens": summary_target_tokens,
                "last_compression_attempt_step": step,
            }
        )
        cache_layout = self._diagnostics.observe(
            step,
            request.messages,
            request.tools,
            request_kind="compression",
            binding_fingerprint=(provider_binding.fingerprint if provider_binding else None),
            provider=provider,
            model=model,
            thinking=thinking,
            epoch_snapshot=self._epoch.diagnostic_snapshot(),
            system_instructions=system_instructions,
            task_project_snapshot=task_project_snapshot,
        )
        prepared = PromptCacheCompressionPreparation(
            step=step,
            epoch_id=self.epoch_id,
            request=request,
            cache_layout=cache_layout,
            source_request_id=source_request_id,
            source_message_count=source_message_count,
            source_epoch_generation=self._epoch.snapshot.generation,
            source_fingerprint=source_fingerprint,
            unsent_suffix=[message.model_copy(deep=True) for message in unsent_suffix_messages],
            candidate_input_tokens=estimated_candidate,
            ordinary_limit=budget.ordinary_limit,
            summary_limit=summary_limit,
            compression_instruction=final_instruction,
            summary_target_tokens=summary_target_tokens,
            memory_message_limit=budget.memory_message_limit,
            candidate_publication_state=candidate_publication_state,
            decision=decision,
        )
        self._append_only_state = next_state
        self._mark_pending("compression", cache_layout.request_fingerprint)
        return prepared

    def observe_compression_response(
        self,
        prepared: PromptCacheCompressionPreparation,
        usage: ModelUsage,
        *,
        account_usage: bool = True,
    ) -> PromptCacheResponseObservation:
        self._ensure_pending("compression", prepared.cache_layout.request_fingerprint)
        if prepared.epoch_id != self.epoch_id:
            raise PromptCacheCoordinatorError("compression response belongs to a different epoch")
        cache_layout = self._diagnostics.finalize(prepared.cache_layout, usage)
        if account_usage:
            self._usage.record(usage)
        self._clear_pending()
        self._compression_ready_epoch = prepared.epoch_id
        return PromptCacheResponseObservation(
            cache_layout=cache_layout,
            cache_usage=self._usage.report_fields(),
        )

    def rollover(
        self,
        summary: str,
        *,
        boundary: CacheEpochBoundary,
        expected_epoch_id: str | None = None,
    ) -> CacheEpochSnapshot:
        if self._epoch is None:
            raise PromptCacheCoordinatorError(
                "cannot roll over an epoch when stable layout is disabled"
            )
        if expected_epoch_id is not None and expected_epoch_id != self.epoch_id:
            raise PromptCacheCoordinatorError("epoch rollover targets the current epoch id")
        if self._compression_ready_epoch != self.epoch_id:
            raise PromptCacheCoordinatorError(
                "epoch rollover requires an observed compression response for the current epoch"
            )
        self._epoch = self._epoch.rollover(summary, boundary=boundary)
        self._cache_epoch_id = self._epoch.epoch_id
        self._prefix_message_count = self._epoch.prefix_message_count
        self._publication = MemoryDeltaPublisher(max_delta_tokens=self._max_delta_tokens)
        self._compression_ready_epoch = None
        return self._epoch.snapshot

    def complete_compression(
        self,
        prepared: PromptCacheCompressionPreparation,
        summary: str,
    ) -> CacheEpochSnapshot:
        if prepared.epoch_id != self.epoch_id:
            raise PromptCacheCoordinatorError("compression summary belongs to a different epoch")
        if self.layout is PromptCacheLayout.APPEND_ONLY:
            return self.complete_append_only_compression(prepared, summary).epoch
        return self.rollover(
            summary,
            boundary=prepared.request.boundary,
            expected_epoch_id=prepared.epoch_id,
        )

    def complete_append_only_compression(
        self,
        prepared: PromptCacheCompressionPreparation,
        summary: str,
    ) -> PromptCacheCompressionCompletion:
        """Validate a bounded new epoch and suffix before committing either state."""

        if self.layout is not PromptCacheLayout.APPEND_ONLY:
            raise PromptCacheCoordinatorError(
                "append-only compression completion requires append_only layout"
            )
        if prepared.epoch_id != self.epoch_id or self._epoch is None:
            raise PromptCacheCoordinatorError("compression summary belongs to a different epoch")
        if self._compression_ready_epoch != self.epoch_id:
            raise PromptCacheCoordinatorError(
                "epoch rollover requires an observed compression response for the current epoch"
            )
        if (
            prepared.source_fingerprint is None
            or prepared.source_request_id is None
            or prepared.source_message_count is None
            or prepared.source_epoch_generation is None
            or prepared.candidate_input_tokens is None
            or prepared.ordinary_limit is None
            or prepared.summary_limit is None
            or prepared.compression_instruction is None
            or prepared.memory_message_limit is None
        ):
            raise PromptCacheCoordinatorError("append-only compression metadata is incomplete")
        self._validate_append_only_compression_preparation(prepared)

        try:
            validated_summary = validate_compression_summary(
                summary,
                guard=self._epoch.content_guard,
                max_summary_tokens=prepared.summary_limit,
            )
        except ValueError as exc:
            raise self._reject_append_only_compression(
                prepared,
                "invalid_summary",
                detail=str(exc),
            ) from exc
        summary_estimated_tokens = ContextEngine.estimate_message(
            ModelMessage(role="assistant", content=validated_summary)
        )

        try:
            candidate_epoch = self._epoch.rollover(
                validated_summary,
                boundary=prepared.request.boundary,
                replace_summary=True,
                root_prefix_message_count=(
                    self._append_only_state.root_prefix_message_count
                    if self._append_only_state is not None
                    else None
                ),
                max_summary_tokens=prepared.summary_limit,
            )
            publication_source = prepared.candidate_publication_state or self._publication.snapshot
            rebased_publication = MemoryDeltaPublisher(
                publication_source,
                max_delta_tokens=self._max_delta_tokens,
            ).rebase_snapshot(
                candidate_epoch.epoch_id,
                max_message_tokens=prepared.memory_message_limit,
                source_state=publication_source,
            )
            rebased_messages = [
                *candidate_epoch.frozen_prefix,
                *[message.model_copy(deep=True) for message in prepared.unsent_suffix],
                *rebased_publication.messages,
            ]
            rebased_window = ContextEngine(
                max_tokens=256,
                max_tool_output_chars=128,
                recent_steps=1,
            ).build_append_only(
                rebased_messages,
                self._frozen_tools,
                max_input_tokens=prepared.ordinary_limit,
            )
        except (ContextBudgetError, MemoryDeltaTooLarge, ValueError) as exc:
            raise self._reject_append_only_compression(prepared, "no_gain") from exc

        rebased_input_tokens = rebased_window.debug.estimated_tokens
        if (
            rebased_input_tokens > prepared.ordinary_limit
            or rebased_input_tokens >= prepared.candidate_input_tokens
        ):
            raise self._reject_append_only_compression(prepared, "no_gain")

        state = self._append_only_state
        if state is None:
            raise PromptCacheCoordinatorError("append-only compression state is unavailable")
        self._epoch = candidate_epoch
        self._cache_epoch_id = candidate_epoch.epoch_id
        self._prefix_message_count = candidate_epoch.prefix_message_count
        self._publication = MemoryDeltaPublisher(
            rebased_publication.next_state,
            max_delta_tokens=self._max_delta_tokens,
        )
        self._append_only_state = AppendOnlyPromptState.model_validate(
            {
                **state.model_dump(mode="python"),
                "last_submitted_message_count": 0,
                "last_submitted_message_fingerprints": [],
                "last_submitted_epoch_generation": candidate_epoch.snapshot.generation,
                "last_submitted_request_id": None,
                "epoch_generation": candidate_epoch.snapshot.generation,
                "compression_source_request_id": prepared.source_request_id,
                "compression_source_message_count": prepared.source_message_count,
                "compression_source_epoch_generation": prepared.source_epoch_generation,
                "compression_request_id": None,
                "compression_max_output_tokens": None,
                "compression_instruction": None,
                "compression_summary_target_tokens": None,
                "deferred_compression_fingerprint": None,
            }
        )
        self._compression_ready_epoch = None
        return PromptCacheCompressionCompletion(
            epoch=candidate_epoch.snapshot,
            messages=rebased_messages,
            publication_state=rebased_publication.next_state,
            source_request_id=prepared.source_request_id,
            source_message_count=prepared.source_message_count,
            source_fingerprint=prepared.source_fingerprint,
            candidate_input_tokens=prepared.candidate_input_tokens,
            rebased_input_tokens=rebased_input_tokens,
            summary_estimated_tokens=summary_estimated_tokens,
            summary_target_tokens=prepared.summary_target_tokens,
        )

    def record_compression_failure(
        self,
        prepared: PromptCacheCompressionPreparation,
        reason: str,
    ) -> CompressionFailureAction:
        """Defer this source after a failed attempt and choose soft/hard handling."""

        if self.layout is not PromptCacheLayout.APPEND_ONLY:
            raise PromptCacheCoordinatorError(
                "append-only compression failures require append_only layout"
            )
        if (
            prepared.epoch_id != self.epoch_id
            or prepared.source_fingerprint is None
            or prepared.candidate_input_tokens is None
            or prepared.ordinary_limit is None
        ):
            raise PromptCacheCoordinatorError("compression failure metadata is incomplete")
        self._validate_append_only_compression_preparation(prepared)
        if not reason:
            raise ValueError("compression failure reason cannot be empty")
        action = (
            CompressionFailureAction.CONTINUE_OLD_EPOCH
            if prepared.candidate_input_tokens <= prepared.ordinary_limit
            else CompressionFailureAction.PAUSE_CONTEXT_BUDGET
        )
        state = self._append_only_state
        if state is None:
            raise PromptCacheCoordinatorError("append-only compression state is unavailable")
        self._append_only_state = AppendOnlyPromptState.model_validate(
            {
                **state.model_dump(mode="python"),
                "deferred_compression_fingerprint": prepared.source_fingerprint,
            }
        )
        self._compression_ready_epoch = None
        return action

    def _validate_append_only_compression_preparation(
        self,
        prepared: PromptCacheCompressionPreparation,
    ) -> None:
        """Ensure a completion or failure still targets the exact submitted source."""

        state = self._append_only_state
        if state is None or self._epoch is None:
            raise PromptCacheCoordinatorError("append-only compression state is unavailable")
        if (
            prepared.epoch_id != self.epoch_id
            or prepared.source_epoch_generation != self._epoch.snapshot.generation
            or state.last_submitted_epoch_generation != self._epoch.snapshot.generation
            or prepared.source_message_count != state.last_submitted_message_count
            or prepared.source_request_id != state.last_submitted_request_id
            or prepared.source_request_id != state.compression_source_request_id
            or prepared.source_message_count != state.compression_source_message_count
            or prepared.source_epoch_generation != state.compression_source_epoch_generation
        ):
            raise PromptCacheCoordinatorError(
                "append-only compression source no longer matches the submitted request"
            )
        if (
            prepared.request.source_request_id != prepared.source_request_id
            or prepared.request.source_message_count != prepared.source_message_count
            or len(prepared.request.messages) != prepared.source_message_count + 1
            or prepared.compression_instruction is None
            or prepared.request.messages[-1].content != prepared.compression_instruction
            or state.compression_instruction != prepared.compression_instruction
            or state.compression_summary_target_tokens != prepared.summary_target_tokens
        ):
            raise PromptCacheCoordinatorError("compression request source boundary is invalid")
        if prepared.decision is not None and (
            prepared.decision.action != "compress"
            or prepared.decision.candidate_input_tokens != prepared.candidate_input_tokens
            or state.last_compression_attempt_step != prepared.step
        ):
            raise PromptCacheCoordinatorError("compression decision metadata is invalid")
        source_messages = prepared.request.messages[:-1]
        source_fingerprints = [_message_fingerprint(message) for message in source_messages]
        if source_fingerprints != state.last_submitted_message_fingerprints:
            raise PromptCacheCoordinatorError(
                "append-only compression source differs from the submitted request"
            )
        expected_fingerprint = _fingerprint_payload(
            {
                "epoch_id": self.epoch_id,
                "messages": [message.model_dump(mode="json") for message in source_messages],
                "tools": [tool.model_dump(mode="json") for tool in self._frozen_tools],
                "binding": state.last_submitted_binding_fingerprint,
            }
        )
        if prepared.source_fingerprint != expected_fingerprint:
            raise PromptCacheCoordinatorError("append-only compression source fingerprint changed")

    def _reject_append_only_compression(
        self,
        prepared: PromptCacheCompressionPreparation,
        reason: str,
        *,
        detail: str | None = None,
    ) -> PromptCompressionRejected:
        action = self.record_compression_failure(prepared, reason)
        return PromptCompressionRejected(reason, action, detail=detail)

    def snapshot(self) -> PromptCacheCoordinatorSnapshot:
        return PromptCacheCoordinatorSnapshot(
            layout=self.layout,
            cache_epoch_id=self._cache_epoch_id,
            prefix_message_count=self._prefix_message_count,
            frozen_tools=self.frozen_tools,
            cache_epoch_state=self._epoch.snapshot if self._epoch is not None else None,
            memory_publication_state=self._publication.snapshot,
            append_only_state=self._append_only_state,
            cache_diagnostics=self._diagnostics.snapshot(),
            cache_usage=self._usage.snapshot(),
            miss_threshold_tokens=self._miss_threshold_tokens,
            max_delta_tokens=self._max_delta_tokens,
        )

    def _ensure_step_available(self, step: int) -> None:
        if step < 0:
            raise PromptCacheCoordinatorError("prompt-cache step must be non-negative")
        if self._pending_kind is not None:
            raise PromptCacheCoordinatorError(
                f"cannot prepare a new request while {self._pending_kind} response is pending"
            )

    def _mark_pending(self, kind: str, fingerprint: str) -> None:
        self._pending_kind = kind
        self._pending_fingerprint = fingerprint

    def _ensure_pending(self, kind: str, fingerprint: str) -> None:
        if self._pending_kind != kind or self._pending_fingerprint != fingerprint:
            raise PromptCacheCoordinatorError(
                f"{kind} response does not match the pending coordinator request"
            )

    def _clear_pending(self) -> None:
        self._pending_kind = None
        self._pending_fingerprint = None

    def _validate_append_only_request(
        self,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
        *,
        provider: str,
        model: str | None,
        thinking: object | None,
        provider_binding: ProviderBinding | None,
    ) -> None:
        state = self._append_only_state
        if state is None or self._epoch is None:
            raise PromptPrefixViolation("append-only boundary state is unavailable")
        if state.root_prefix_message_count > self._epoch.prefix_message_count:
            raise PromptPrefixViolation("declared root prefix exceeds the frozen epoch")
        if len(messages) < self._epoch.prefix_message_count:
            raise PromptPrefixViolation("request is shorter than the frozen epoch prefix")
        same_submitted_epoch = (
            state.last_submitted_epoch_generation == self._epoch.snapshot.generation
        )
        if same_submitted_epoch and len(messages) < state.last_submitted_message_count:
            raise PromptPrefixViolation("request omits messages from the previous request")

        request_prefix_fingerprints = [
            _message_fingerprint(message)
            for message in messages[: self._epoch.prefix_message_count]
        ]
        frozen_fingerprints = [
            _message_fingerprint(message) for message in self._epoch.frozen_prefix
        ]
        for index, (actual, expected) in enumerate(
            zip(request_prefix_fingerprints, frozen_fingerprints, strict=True)
        ):
            if actual != expected:
                raise PromptPrefixViolation(
                    "frozen epoch message changed",
                    message_index=index,
                    expected_fingerprint=expected,
                    actual_fingerprint=actual,
                )

        if same_submitted_epoch:
            current_fingerprints = [_message_fingerprint(message) for message in messages]
            for index, expected in enumerate(state.last_submitted_message_fingerprints):
                actual = current_fingerprints[index]
                if actual != expected:
                    raise PromptPrefixViolation(
                        "previously submitted message changed",
                        message_index=index,
                        expected_fingerprint=expected,
                        actual_fingerprint=actual,
                    )

        expected_tools = _fingerprint_payload(
            [tool.model_dump(mode="json") for tool in self._frozen_tools]
        )
        actual_tools = _fingerprint_payload([tool.model_dump(mode="json") for tool in tools])
        if actual_tools != expected_tools:
            raise PromptPrefixViolation(
                "tool definitions or order changed",
                expected_fingerprint=expected_tools,
                actual_fingerprint=actual_tools,
            )
        if (
            state.last_submitted_tool_fingerprint is not None
            and actual_tools != state.last_submitted_tool_fingerprint
        ):
            raise PromptPrefixViolation(
                "tool definitions changed since the previous request",
                expected_fingerprint=state.last_submitted_tool_fingerprint,
                actual_fingerprint=actual_tools,
            )

        binding_fingerprint = _binding_fingerprint(
            provider,
            model,
            thinking,
            provider_binding,
        )
        if (
            state.last_submitted_binding_fingerprint is not None
            and binding_fingerprint != state.last_submitted_binding_fingerprint
        ):
            raise PromptPrefixViolation(
                "provider binding changed since the previous request",
                expected_fingerprint=state.last_submitted_binding_fingerprint,
                actual_fingerprint=binding_fingerprint,
            )

    def _record_append_only_submission(
        self,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
        *,
        provider: str,
        model: str | None,
        thinking: object | None,
        provider_binding: ProviderBinding | None,
        request_id: str | None,
    ) -> None:
        state = self._append_only_state
        if state is None:
            raise PromptPrefixViolation("append-only boundary state is unavailable")
        self._append_only_state = AppendOnlyPromptState.model_validate(
            {
                **state.model_dump(mode="python"),
                "last_submitted_message_count": len(messages),
                "last_submitted_message_fingerprints": [
                    _message_fingerprint(message) for message in messages
                ],
                "last_submitted_epoch_generation": (
                    self._epoch.snapshot.generation if self._epoch is not None else 0
                ),
                "last_submitted_tool_fingerprint": _fingerprint_payload(
                    [tool.model_dump(mode="json") for tool in tools]
                ),
                "last_submitted_binding_fingerprint": _binding_fingerprint(
                    provider,
                    model,
                    thinking,
                    provider_binding,
                ),
                "last_submitted_request_id": request_id,
            }
        )

    def abort_pending(self) -> None:
        """Discard an in-flight provider response after an external failure."""

        self._clear_pending()

    def commit_publication(self, snapshot: MemoryPublicationSnapshot) -> None:
        """Install a validated transcript publication with its checkpoint candidate."""
        if self.layout is not PromptCacheLayout.APPEND_ONLY or (
            snapshot.schema_version != "2.0" or snapshot.epoch_id != self.epoch_id
        ):
            raise PromptCacheCoordinatorError("publication does not match append-only epoch")
        self._publication = MemoryDeltaPublisher(snapshot, max_delta_tokens=self._max_delta_tokens)

    def set_compression_request_id(
        self,
        request_id: str | None,
        *,
        max_output_tokens: int | None = None,
        instruction: str | None = None,
        summary_target_tokens: int | None = None,
    ) -> None:
        """Associate the pending compression with the existing Provider journal."""
        if self._append_only_state is None:
            raise PromptCacheCoordinatorError("append-only state is unavailable")
        state = self._append_only_state
        if request_id is not None:
            frozen_instruction = instruction or state.compression_instruction
            if frozen_instruction is None:
                frozen_instruction = COMPRESSION_INSTRUCTION
            if (
                state.compression_instruction is not None
                and state.compression_instruction != frozen_instruction
            ):
                raise PromptCacheCoordinatorError("compression instruction changed while pending")
            frozen_target = (
                summary_target_tokens
                if summary_target_tokens is not None
                else state.compression_summary_target_tokens
            )
            if (
                state.compression_summary_target_tokens is not None
                and state.compression_summary_target_tokens != frozen_target
            ):
                raise PromptCacheCoordinatorError(
                    "compression summary target changed while pending"
                )
        else:
            frozen_instruction = None
            frozen_target = None
        self._append_only_state = AppendOnlyPromptState.model_validate(
            {
                **state.model_dump(),
                "compression_request_id": request_id,
                "compression_max_output_tokens": max_output_tokens if request_id else None,
                "compression_instruction": frozen_instruction,
                "compression_summary_target_tokens": frozen_target,
            }
        )


__all__ = [
    "AppendOnlyOptimizationPolicy",
    "AppendOnlyOptimizationVersion",
    "AppendOnlyPromptState",
    "PrefixBudget",
    "PromptCacheCheckpointFields",
    "PromptCacheCompressionPreparation",
    "PromptCacheCoordinator",
    "PromptCacheCoordinatorError",
    "PromptCacheCoordinatorSnapshot",
    "PromptCachePreparedRequest",
    "PromptCacheResponseObservation",
    "PromptPrefixViolation",
    "compute_prefix_budget",
]
