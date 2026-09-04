"""Pure lifecycle coordination for prompt-cache state."""

from __future__ import annotations

from typing import Self

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
    CacheUsageReportFields,
)
from patchloop.providers.base import ModelMessage, ModelUsage, ToolSpec
from patchloop.security import SecretRedactor


class PromptCacheCoordinatorError(ValueError):
    """Raised when a prompt-cache lifecycle transition is invalid."""


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
    cache_diagnostics: CacheDiagnosticsSnapshot
    cache_usage: CacheUsageAccumulatorSnapshot
    miss_threshold_tokens: int = Field(default=70_000, ge=1)
    max_delta_tokens: int = Field(default=2_048, ge=64)

    @model_validator(mode="after")
    def validate_layout_state(self) -> Self:
        if self.layout is PromptCacheLayout.STABLE and self.cache_epoch_state is None:
            raise ValueError("stable prompt-cache coordinator requires an epoch snapshot")
        if self.layout is PromptCacheLayout.LEGACY and self.cache_epoch_state is not None:
            raise ValueError("legacy prompt-cache coordinator cannot carry an epoch snapshot")
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
        if layout is PromptCacheLayout.STABLE and cache_epoch is None:
            raise ValueError("stable prompt-cache coordinator requires an epoch")
        if layout is PromptCacheLayout.LEGACY and cache_epoch is not None:
            raise ValueError("legacy prompt-cache coordinator cannot carry an epoch")
        self.layout = layout
        self._cache_epoch_id = cache_epoch_id
        self._prefix_message_count = prefix_message_count
        self._frozen_tools = [tool.model_copy(deep=True) for tool in frozen_tools]
        self._epoch = cache_epoch
        self._publication = publication or MemoryDeltaPublisher(max_delta_tokens=max_delta_tokens)
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
    ) -> PromptCacheCoordinator:
        prefix_count = len(messages) if prefix_message_count is None else prefix_message_count
        epoch = (
            CacheEpoch.bootstrap(
                messages,
                prefix_message_count=prefix_count,
                epoch_id=epoch_id,
                redactor=redactor,
            )
            if layout is PromptCacheLayout.STABLE
            else None
        )
        return cls(
            layout=layout,
            cache_epoch_id=epoch_id,
            prefix_message_count=prefix_count,
            frozen_tools=PromptLayout.freeze_tools(tools),
            cache_epoch=epoch,
            redactor=redactor,
            miss_threshold_tokens=miss_threshold_tokens,
            max_delta_tokens=max_delta_tokens,
        )

    start = bootstrap

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
    def usage(self) -> CacheUsageAccumulator:
        return self._usage

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
        request_messages = [message.model_copy(deep=True) for message in messages]
        published_memory = memory_projection
        if self._epoch is not None:
            request_messages = self._epoch.materialize(request_messages)
            if memory_projection is not None:
                self._publication, _ = self._publication.publish(self.epoch_id, memory_projection)
            if self._publication.snapshot is not None:
                publication_messages = self._publication.messages
                request_messages = [
                    *request_messages[: self.prefix_message_count],
                    *publication_messages,
                    *request_messages[self.prefix_message_count :],
                ]
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


__all__ = [
    "PromptCacheCompressionPreparation",
    "PromptCacheCoordinator",
    "PromptCacheCoordinatorError",
    "PromptCacheCoordinatorSnapshot",
    "PromptCachePreparedRequest",
    "PromptCacheResponseObservation",
]
