"""Pure lifecycle coordination for prompt-cache state."""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.domain import PromptCacheLayout
from patchloop.prompt_cache.diagnostics import (
    CacheDiagnostics,
    CacheDiagnosticsSnapshot,
    CacheLayoutTrace,
)
from patchloop.prompt_cache.epoch import (
    CacheCompressionRequest,
    CacheEpoch,
    CacheEpochBoundary,
    CacheEpochSnapshot,
)
from patchloop.prompt_cache.layout import PromptLayout
from patchloop.prompt_cache.publication import (
    MemoryDeltaPublisher,
    MemoryPublicationSnapshot,
)
from patchloop.prompt_cache.usage import (
    CacheUsageAccumulator,
    CacheUsageAccumulatorSnapshot,
    CacheUsageCheckpointFields,
    CacheUsageReportFields,
)
from patchloop.providers.base import ModelMessage, ModelUsage, ToolSpec
from patchloop.security import SecretRedactor

_SHA256_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")

# Layouts whose coordinator owns a frozen cache epoch; legacy keeps none.
_FROZEN_EPOCH_LAYOUTS = frozenset({PromptCacheLayout.STABLE, PromptCacheLayout.APPEND_ONLY})


class PromptCacheCoordinatorError(ValueError):
    """Raised when a prompt-cache lifecycle transition is invalid."""


class AppendOnlyPromptState(BaseModel):
    """Recoverable boundary state of the append_only single-transcript contract.

    Message bodies live only in the checkpoint transcript; this state records
    counts and fingerprints so a restored process can verify that the previous
    submitted request is still an exact prefix without a second copy.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    root_prefix_message_count: int = Field(ge=2)
    last_submitted_message_count: int = Field(default=0, ge=0)
    last_submitted_message_fingerprints: list[str] = Field(default_factory=list)
    last_submitted_tool_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    last_submitted_binding_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    last_submitted_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    epoch_generation: int = Field(default=0, ge=0)
    compression_source_request_id: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    compression_source_message_count: int | None = Field(default=None, ge=0)
    compression_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    deferred_compression_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

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
            and self.compression_source_message_count > self.last_submitted_message_count
        ):
            raise ValueError(
                "compression source cannot exceed the last submitted message boundary"
            )
        return self

    def validate_message_boundaries(self, message_count: int) -> None:
        """Cross-check declared boundaries against the persisted transcript length."""

        if message_count < self.root_prefix_message_count:
            raise ValueError("append-only transcript is shorter than its declared root prefix")
        if message_count < self.last_submitted_message_count:
            raise ValueError(
                "append-only transcript is shorter than its last submitted request"
            )


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
            raise ValueError(
                "append_only prompt-cache coordinator requires append-only state"
            )
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
            raise ValueError(
                "append_only prompt-cache coordinator requires append-only state"
            )
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
    ) -> PromptCacheCoordinator:
        prefix_count = len(messages) if prefix_message_count is None else prefix_message_count
        epoch = (
            CacheEpoch.bootstrap(
                messages,
                prefix_message_count=prefix_count,
                epoch_id=epoch_id,
                redactor=redactor,
            )
            if PromptLayout(layout).has_frozen_epoch
            else None
        )
        if layout is PromptCacheLayout.APPEND_ONLY and append_only_state is None:
            append_only_state = AppendOnlyPromptState(root_prefix_message_count=prefix_count)
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
                CacheEpoch.from_snapshot(epoch, redactor=redactor) if epoch is not None else None
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
                CacheEpoch.from_snapshot(snapshot.cache_epoch_state, redactor=redactor)
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
        system_instructions: str | None = None,
        task_project_snapshot: object | None = None,
        memory_projection: str | None = None,
    ) -> PromptCachePreparedRequest:
        self._ensure_step_available(step)
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
            self._frozen_tools,
            provider=provider,
            model=model,
            thinking=thinking,
            epoch_snapshot=(
                self._epoch.diagnostic_snapshot() if self._epoch is not None else self.epoch_id
            ),
            system_instructions=system_instructions,
            task_project_snapshot=task_project_snapshot,
            memory_projection=published_memory,
        )
        prepared = PromptCachePreparedRequest(
            step=step,
            epoch_id=self.epoch_id,
            messages=request_messages,
            tools=self.frozen_tools,
            cache_layout=cache_layout,
        )
        self._mark_pending("request", cache_layout.request_fingerprint)
        return prepared

    def observe_response(
        self,
        prepared: PromptCachePreparedRequest,
        usage: ModelUsage,
    ) -> PromptCacheResponseObservation:
        self._ensure_pending("request", prepared.cache_layout.request_fingerprint)
        cache_layout = self._diagnostics.finalize(prepared.cache_layout, usage)
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
    ) -> PromptCacheCompressionPreparation:
        self._ensure_step_available(step)
        if self._epoch is None:
            raise PromptCacheCoordinatorError(
                "cannot prepare epoch compression when stable layout is disabled"
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

    def observe_compression_response(
        self,
        prepared: PromptCacheCompressionPreparation,
        usage: ModelUsage,
    ) -> PromptCacheResponseObservation:
        self._ensure_pending("compression", prepared.cache_layout.request_fingerprint)
        if prepared.epoch_id != self.epoch_id:
            raise PromptCacheCoordinatorError("compression response belongs to a different epoch")
        cache_layout = self._diagnostics.finalize(prepared.cache_layout, usage)
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
        return self.rollover(
            summary,
            boundary=prepared.request.boundary,
            expected_epoch_id=prepared.epoch_id,
        )

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

    def abort_pending(self) -> None:
        """Discard an in-flight provider response after an external failure."""

        self._clear_pending()


__all__ = [
    "AppendOnlyPromptState",
    "PromptCacheCheckpointFields",
    "PromptCacheCompressionPreparation",
    "PromptCacheCoordinator",
    "PromptCacheCoordinatorError",
    "PromptCacheCoordinatorSnapshot",
    "PromptCachePreparedRequest",
    "PromptCacheResponseObservation",
]
