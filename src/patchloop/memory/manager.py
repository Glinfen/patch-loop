"""Unified ingestion, retrieval, compression, and recovery for layered memory."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.domain import Plan, ToolCall, ToolResult
from patchloop.memory.compression import CompressionBatch, MemoryCompressor
from patchloop.memory.episodic import (
    EpisodeWrite,
    EpisodicMemoryManager,
    EpisodicMemorySnapshot,
)
from patchloop.memory.models import (
    CompressionReport,
    MemoryKind,
    MemoryRecord,
    MemorySource,
    MemoryStatus,
)
from patchloop.memory.retrieval import CrossLayerMemoryRetriever, LayeredMemoryContext
from patchloop.memory.semantic import SemanticMemoryManager, SemanticResolutionBatch
from patchloop.memory.store import MemoryStoreError
from patchloop.memory.working import (
    MemoryPromotionBatch,
    WorkingMemoryManager,
    WorkingMemorySnapshot,
)


class MemoryManagerStore(Protocol):
    def list_records(self, task_id: str) -> list[MemoryRecord]: ...

    def list_sources(self, task_id: str) -> list[MemorySource]: ...

    def save_batch(
        self,
        *,
        sources: Sequence[MemorySource] = (),
        records: Sequence[MemoryRecord] = (),
        compactions: Sequence[CompressionReport] = (),
    ) -> object: ...


class MemoryCompressionPolicy(BaseModel):
    """Deterministic watermarks that bound automatic compression work."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    active_uncompressed_threshold: int = Field(default=64, ge=2)
    active_generation_threshold: int = Field(default=8, ge=2)
    maximum_generation: int = Field(default=3, ge=1, le=20)


class MemoryEventCursor(BaseModel):
    """Checkpointable position in the synchronous memory event stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    next_event_index: int = Field(default=0, ge=0)
    last_event_id: str | None = None
    processed_event_ids: list[str] = Field(default_factory=list, max_length=4_096)
    pending_event_ids: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def validate_cursor(self) -> Self:
        if len(self.processed_event_ids) != len(set(self.processed_event_ids)):
            raise ValueError("memory cursor processed event ids must be unique")
        if len(self.pending_event_ids) != len(set(self.pending_event_ids)):
            raise ValueError("memory cursor pending event ids must be unique")
        if set(self.processed_event_ids) & set(self.pending_event_ids):
            raise ValueError("memory event cannot be both processed and pending")
        if self.next_event_index != len(self.processed_event_ids):
            raise ValueError("memory cursor index must equal processed event count")
        if bool(self.processed_event_ids) != bool(self.last_event_id):
            raise ValueError("memory cursor last event must match processed events")
        if self.processed_event_ids and self.last_event_id != self.processed_event_ids[-1]:
            raise ValueError("memory cursor last event must be the final processed event")
        return self


class MemoryManagerSnapshot(BaseModel):
    """Complete recovery boundary for unified Memory 2.0 state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=1)
    cursor: MemoryEventCursor = Field(default_factory=MemoryEventCursor)
    working_memory: WorkingMemorySnapshot
    episodic_memory: EpisodicMemorySnapshot
    records_written: int = Field(default=0, ge=0)
    compactions: int = Field(default=0, ge=0)
    compression_input_tokens: int = Field(default=0, ge=0)
    compression_output_tokens: int = Field(default=0, ge=0)
    fallback_count: int = Field(default=0, ge=0)
    fallback_active: bool = False
    fallback_reason: str | None = None
    records_by_kind: dict[str, int] = Field(default_factory=dict)
    records_by_status: dict[str, int] = Field(default_factory=dict)
    read_duration_ms: float = Field(default=0.0, ge=0.0)
    write_duration_ms: float = Field(default=0.0, ge=0.0)
    compression_duration_ms: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if self.working_memory.task_id != self.task_id:
            raise ValueError("working memory snapshot belongs to another manager task")
        if self.episodic_memory.task_id != self.task_id:
            raise ValueError("episodic memory snapshot belongs to another manager task")
        if self.fallback_active != bool(self.fallback_reason):
            raise ValueError("memory fallback state and reason must be set together")
        if self.compression_output_tokens > self.compression_input_tokens:
            raise ValueError("memory compression output cannot exceed input")
        if any(
            value < 0
            for value in (*self.records_by_kind.values(), *self.records_by_status.values())
        ):
            raise ValueError("memory inventory counts cannot be negative")
        return self


@dataclass(frozen=True)
class MemoryManagerUpdate:
    event_id: str
    event_index: int
    duplicate: bool = False
    promotion: MemoryPromotionBatch | None = None
    episode: EpisodeWrite | None = None
    semantic: SemanticResolutionBatch | None = None
    written_record_ids: tuple[str, ...] = ()
    compaction: CompressionBatch | None = None
    fallback_reason: str | None = None
    write_duration_ms: float = 0.0
    compression_duration_ms: float = 0.0


@dataclass(frozen=True)
class ManagedMemoryRetrieval:
    context: LayeredMemoryContext | None
    fallback_reason: str | None = None
    read_duration_ms: float = 0.0


class MemoryManager:
    """Own all Memory 2.0 state behind a replay-safe event boundary."""

    def __init__(
        self,
        task_id: str,
        goal: str,
        repository_scope_id: str,
        *,
        working_token_budget: int,
        plan: Plan | None = None,
        step_index: int = 0,
        store: MemoryManagerStore | None = None,
        snapshot: MemoryManagerSnapshot | None = None,
        legacy_working: WorkingMemorySnapshot | None = None,
        legacy_episodic: EpisodicMemorySnapshot | None = None,
        compression_policy: MemoryCompressionPolicy | None = None,
    ) -> None:
        if snapshot is not None and snapshot.task_id != task_id:
            raise ValueError("memory manager snapshot belongs to another task")
        self.task_id = task_id
        self.goal = goal
        self.repository_scope_id = repository_scope_id
        self.store = store
        self.compression_policy = compression_policy or MemoryCompressionPolicy()
        self.compressor = MemoryCompressor()
        self.retriever = CrossLayerMemoryRetriever()
        self._fallback_active = snapshot.fallback_active if snapshot is not None else False
        self._fallback_reason = snapshot.fallback_reason if snapshot is not None else None
        self._pending_fallback_reason: str | None = None
        self._records_written = snapshot.records_written if snapshot is not None else 0
        self._compactions = snapshot.compactions if snapshot is not None else 0
        self._compression_input_tokens = (
            snapshot.compression_input_tokens if snapshot is not None else 0
        )
        self._compression_output_tokens = (
            snapshot.compression_output_tokens if snapshot is not None else 0
        )
        self._fallback_count = snapshot.fallback_count if snapshot is not None else 0
        self._records_by_kind = dict(snapshot.records_by_kind) if snapshot is not None else {}
        self._records_by_status = dict(snapshot.records_by_status) if snapshot is not None else {}
        self._read_duration_ms = snapshot.read_duration_ms if snapshot is not None else 0.0
        self._write_duration_ms = snapshot.write_duration_ms if snapshot is not None else 0.0
        self._compression_duration_ms = (
            snapshot.compression_duration_ms if snapshot is not None else 0.0
        )
        cursor = snapshot.cursor if snapshot is not None else None
        self._processed_event_ids = list(cursor.processed_event_ids) if cursor is not None else []
        self._pending_event_ids = list(cursor.pending_event_ids) if cursor is not None else []
        self._volatile_sources: dict[str, MemorySource] = {}
        self._volatile_records: dict[str, MemoryRecord] = {}
        recovered_records: list[MemoryRecord] = []
        if store is not None and not self._fallback_active:
            read_started = perf_counter()
            try:
                recovered_records = store.list_records(task_id)
                recovered_sources = store.list_sources(task_id)
                self._volatile_records.update((record.id, record) for record in recovered_records)
                self._volatile_sources.update((source.id, source) for source in recovered_sources)
            except MemoryStoreError as exc:
                self._activate_fallback(f"memory restore failed: {exc}")
            finally:
                self._read_duration_ms += (perf_counter() - read_started) * 1_000

        working_snapshot = snapshot.working_memory if snapshot is not None else legacy_working
        episodic_snapshot = snapshot.episodic_memory if snapshot is not None else legacy_episodic
        self.working = WorkingMemoryManager(
            task_id,
            goal,
            token_budget=working_token_budget,
            snapshot=working_snapshot,
        )
        if working_snapshot is None:
            self.working.sync_plan(plan, step_index=step_index)
        self.episodic = EpisodicMemoryManager(
            task_id,
            goal,
            snapshot=episodic_snapshot,
            records=recovered_records if episodic_snapshot is None else None,
        )
        self.semantic = SemanticMemoryManager(
            task_id,
            goal,
            repository_scope_id,
            records=recovered_records,
        )
        if snapshot is None:
            self._restore_legacy_cursor(episodic_snapshot, recovered_records)
        self._refresh_inventory()

    @property
    def fallback_active(self) -> bool:
        return self._fallback_active

    def take_fallback_transition(self) -> str | None:
        """Return a new fallback reason once so Runtime can emit one audit event."""

        reason = self._pending_fallback_reason
        self._pending_fallback_reason = None
        return reason

    def inactive_context_values(self) -> list[str]:
        """Return obsolete semantic values that must not leak through raw history."""

        active_values = {
            value
            for record in self._volatile_records.values()
            if record.status is MemoryStatus.ACTIVE
            if isinstance((value := record.content.get("value")), str)
        }
        return sorted(
            {
                value
                for record in self._volatile_records.values()
                if record.status is not MemoryStatus.ACTIVE
                if isinstance((value := record.content.get("value")), str)
                and len(value) >= 4
                and value not in active_values
            },
            key=lambda value: (-len(value), value),
        )

    def ingest_initial(self) -> MemoryManagerUpdate:
        event_id = f"task:{self.task_id}:goal"
        if event_id in self._processed_event_ids:
            return self._duplicate_update(event_id)
        semantic = self.semantic.initial_facts()
        return self._complete_event(event_id, semantic=semantic)

    def ingest_tool(
        self,
        call: ToolCall,
        result: ToolResult,
        *,
        step_index: int,
        plan: Plan | None,
        changed_paths: list[str],
        diff: str,
    ) -> MemoryManagerUpdate:
        event_id = f"tool:{call.id}"
        if event_id in self._processed_event_ids:
            return self._duplicate_update(event_id)
        self._begin_event(event_id)
        promotion = self.working.observe_tool(
            call,
            result,
            step_index=step_index,
            plan=plan,
            changed_paths=changed_paths,
        )
        episode = self.episodic.observe_tool(
            call,
            result,
            step_index=step_index,
            plan=plan,
            changed_paths=changed_paths,
        )
        semantic = self.semantic.observe_tool(
            call,
            result,
            step_index=step_index,
            plan=plan,
            changed_paths=changed_paths,
            diff=diff,
        )
        return self._complete_event(
            event_id,
            promotion=promotion,
            episode=episode,
            semantic=semantic,
        )

    def ingest_checkpoint(
        self,
        *,
        step_index: int,
        plan: Plan | None,
        changed_paths: list[str],
    ) -> MemoryManagerUpdate:
        event_id = f"checkpoint:{self.task_id}:{step_index}"
        if event_id in self._processed_event_ids:
            return self._duplicate_update(event_id)
        self._begin_event(event_id)
        episode = self.episodic.observe_checkpoint(
            step_index=step_index,
            plan=plan,
            changed_paths=changed_paths,
        )
        return self._complete_event(event_id, episode=episode)

    def retrieve(
        self,
        *,
        plan: Plan | None,
        changed_paths: list[str],
        total_context_tokens: int,
        retrieval_token_cap: int | None = None,
    ) -> ManagedMemoryRetrieval:
        if self._fallback_active:
            return ManagedMemoryRetrieval(context=None)
        read_started = perf_counter()
        records = list(self._volatile_records.values())
        sources = list(self._volatile_sources.values())
        if self.store is not None:
            try:
                records = self.store.list_records(self.task_id)
                sources = self.store.list_sources(self.task_id)
            except MemoryStoreError as exc:
                duration = (perf_counter() - read_started) * 1_000
                self._read_duration_ms += duration
                reason = self._activate_fallback(f"memory retrieval failed: {exc}")
                return ManagedMemoryRetrieval(
                    context=None,
                    fallback_reason=reason,
                    read_duration_ms=duration,
                )
        context = self.retriever.retrieve(
            task_id=self.task_id,
            repository_scope_id=self.repository_scope_id,
            goal=self.goal,
            plan=plan,
            working=self.working.snapshot(),
            working_render=self.working.render(),
            episodic_render=self.episodic.render() if self.episodic.has_context() else None,
            changed_paths=changed_paths,
            records=records,
            sources=sources,
            total_context_tokens=total_context_tokens,
            retrieval_token_cap=retrieval_token_cap,
        )
        if not context.rendered and context.omitted_ids:
            duration = (perf_counter() - read_started) * 1_000
            self._read_duration_ms += duration
            return ManagedMemoryRetrieval(context=None, read_duration_ms=duration)
        duration = (perf_counter() - read_started) * 1_000
        self._read_duration_ms += duration
        return ManagedMemoryRetrieval(context=context, read_duration_ms=duration)

    def snapshot(self) -> MemoryManagerSnapshot:
        processed = list(self._processed_event_ids)
        return MemoryManagerSnapshot(
            task_id=self.task_id,
            cursor=MemoryEventCursor(
                next_event_index=len(processed),
                last_event_id=processed[-1] if processed else None,
                processed_event_ids=processed,
                pending_event_ids=list(self._pending_event_ids),
            ),
            working_memory=self.working.snapshot(),
            episodic_memory=self.episodic.snapshot(),
            records_written=self._records_written,
            compactions=self._compactions,
            compression_input_tokens=self._compression_input_tokens,
            compression_output_tokens=self._compression_output_tokens,
            fallback_count=self._fallback_count,
            fallback_active=self._fallback_active,
            fallback_reason=self._fallback_reason,
            records_by_kind=self._records_by_kind,
            records_by_status=self._records_by_status,
            read_duration_ms=self._read_duration_ms,
            write_duration_ms=self._write_duration_ms,
            compression_duration_ms=self._compression_duration_ms,
        )

    def _complete_event(
        self,
        event_id: str,
        *,
        promotion: MemoryPromotionBatch | None = None,
        episode: EpisodeWrite | None = None,
        semantic: SemanticResolutionBatch | None = None,
    ) -> MemoryManagerUpdate:
        self._begin_event(event_id)
        sources = [*(promotion.sources if promotion is not None else ())]
        records = [*(promotion.records if promotion is not None else ())]
        if episode is not None:
            sources.extend(episode.sources)
            records.append(episode.record)
        if semantic is not None:
            sources.extend(semantic.sources)
            records.extend(semantic.records)
        self._remember(sources, records)
        write_duration_ms = 0.0
        if records and self.store is not None and not self._fallback_active:
            write_started = perf_counter()
            try:
                self.store.save_batch(sources=sources, records=records)
            except MemoryStoreError as exc:
                self._activate_fallback(f"memory write failed: {exc}")
            finally:
                write_duration_ms = (perf_counter() - write_started) * 1_000
                self._write_duration_ms += write_duration_ms
        self._records_written += len(records)
        compaction, compression_duration_ms, compression_write_duration_ms = self._maybe_compress()
        write_duration_ms += compression_write_duration_ms
        self._finish_event(event_id)
        return MemoryManagerUpdate(
            event_id=event_id,
            event_index=len(self._processed_event_ids) - 1,
            promotion=promotion,
            episode=episode,
            semantic=semantic,
            written_record_ids=tuple(record.id for record in records),
            compaction=compaction,
            fallback_reason=self.take_fallback_transition(),
            write_duration_ms=write_duration_ms,
            compression_duration_ms=compression_duration_ms,
        )

    def _maybe_compress(self) -> tuple[CompressionBatch | None, float, float]:
        if self._fallback_active:
            return None, 0.0, 0.0
        active = [
            record
            for record in self._volatile_records.values()
            if record.status is MemoryStatus.ACTIVE
        ]
        uncompressed = [record for record in active if record.compression_generation == 0]
        compressed = [record for record in active if record.compression_generation > 0]
        generation_rollup = False
        if len(uncompressed) < self.compression_policy.active_uncompressed_threshold:
            if len(compressed) < self.compression_policy.active_generation_threshold:
                return None, 0.0, 0.0
            if max(record.compression_generation for record in compressed) >= (
                self.compression_policy.maximum_generation
            ):
                return None, 0.0, 0.0
            generation_rollup = True
        compression_started = perf_counter()
        batch = self.compressor.compress(
            self.task_id,
            active,
            list(self._volatile_sources.values()),
            generation_rollup=generation_rollup,
        )
        if batch.report is None:
            duration = (perf_counter() - compression_started) * 1_000
            self._compression_duration_ms += duration
            return None, duration, 0.0
        write_duration = 0.0
        if self.store is not None:
            write_started = perf_counter()
            try:
                self.store.save_batch(records=batch.writes, compactions=[batch.report])
            except (MemoryStoreError, ValueError) as exc:
                write_duration = (perf_counter() - write_started) * 1_000
                self._write_duration_ms += write_duration
                duration = (perf_counter() - compression_started) * 1_000
                self._compression_duration_ms += duration
                self._activate_fallback(f"memory compression failed: {exc}")
                return None, duration, write_duration
            write_duration = (perf_counter() - write_started) * 1_000
            self._write_duration_ms += write_duration
        self._remember((), batch.writes)
        self.semantic.synchronize_records(list(self._volatile_records.values()))
        self._compactions += 1
        self._compression_input_tokens += batch.report.input_tokens
        self._compression_output_tokens += batch.report.output_tokens
        self._records_written += len(batch.summary_records)
        duration = (perf_counter() - compression_started) * 1_000
        self._compression_duration_ms += duration
        return batch, duration, write_duration

    def _remember(
        self,
        sources: Sequence[MemorySource],
        records: Sequence[MemoryRecord],
    ) -> None:
        self._volatile_sources.update((source.id, source) for source in sources)
        self._volatile_records.update((record.id, record) for record in records)
        self._refresh_inventory()

    def _refresh_inventory(self) -> None:
        if not self._volatile_records:
            return
        self._records_by_kind = {
            kind.value: sum(record.kind is kind for record in self._volatile_records.values())
            for kind in MemoryKind
        }
        self._records_by_status = {
            status.value: sum(record.status is status for record in self._volatile_records.values())
            for status in MemoryStatus
        }

    def _begin_event(self, event_id: str) -> None:
        if event_id not in self._pending_event_ids and event_id not in self._processed_event_ids:
            self._pending_event_ids.append(event_id)

    def _finish_event(self, event_id: str) -> None:
        if event_id in self._pending_event_ids:
            self._pending_event_ids.remove(event_id)
        if event_id not in self._processed_event_ids:
            self._processed_event_ids.append(event_id)

    def _duplicate_update(self, event_id: str) -> MemoryManagerUpdate:
        return MemoryManagerUpdate(
            event_id=event_id,
            event_index=self._processed_event_ids.index(event_id),
            duplicate=True,
            fallback_reason=self.take_fallback_transition(),
        )

    def _activate_fallback(self, reason: str) -> str | None:
        if self._fallback_active:
            return None
        self._fallback_active = True
        self._fallback_reason = reason
        self._pending_fallback_reason = reason
        self._fallback_count += 1
        return reason

    def _restore_legacy_cursor(
        self,
        episodic: EpisodicMemorySnapshot | None,
        records: Sequence[MemoryRecord],
    ) -> None:
        if any(record.kind.value == "semantic" for record in records):
            self._processed_event_ids.append(f"task:{self.task_id}:goal")
        if episodic is None:
            episodic = self.episodic.snapshot()
        self._processed_event_ids.extend(
            event_id
            for event_id in (
                *[f"tool:{call_id}" for call_id in episodic.recorded_call_ids],
                *[
                    f"checkpoint:{self.task_id}:{step}"
                    for step in episodic.recorded_checkpoint_steps
                ],
            )
            if event_id not in self._processed_event_ids
        )
