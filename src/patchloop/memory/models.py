"""Versioned domain contracts for PatchLoop's layered memory system."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from patchloop.domain import ErrorKind, utc_now

MemorySchemaVersion = Literal["1.0"]
MEMORY_SCHEMA_VERSION: MemorySchemaVersion = "1.0"


def _identifier() -> str:
    return str(uuid4())


class VersionedMemoryModel(BaseModel):
    """Strict V1 wire contract shared by every persisted memory model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: MemorySchemaVersion = MEMORY_SCHEMA_VERSION


class MemoryKind(StrEnum):
    WORKING = "working"
    SEMANTIC = "semantic"
    EPISODIC = "episodic"


def _all_memory_kinds() -> list[MemoryKind]:
    return list(MemoryKind)


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"


class MemoryScope(StrEnum):
    TASK = "task"
    REPOSITORY = "repository"


class MemorySourceKind(StrEnum):
    EVENT = "event"
    TOOL_RESULT = "tool_result"
    USER_MESSAGE = "user_message"
    CHECKPOINT = "checkpoint"
    MEMORY_RECORD = "memory_record"


class CompressionOperation(StrEnum):
    DEDUPLICATE = "deduplicate"
    MERGE = "merge"
    SUMMARIZE = "summarize"
    PRUNE = "prune"


class MemorySource(VersionedMemoryModel):
    """Immutable pointer to evidence retained outside derived memory."""

    id: str = Field(default_factory=_identifier, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
    task_id: str = Field(min_length=1)
    kind: MemorySourceKind
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    event_id: str | None = None
    step_index: int | None = Field(default=None, ge=0)
    tool_call_id: str | None = None
    checkpoint_id: str | None = None
    memory_record_id: str | None = None
    path: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    captured_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_locator(self) -> Self:
        required_locator = {
            MemorySourceKind.EVENT: self.event_id,
            MemorySourceKind.TOOL_RESULT: self.tool_call_id,
            MemorySourceKind.USER_MESSAGE: self.event_id,
            MemorySourceKind.CHECKPOINT: self.checkpoint_id,
            MemorySourceKind.MEMORY_RECORD: self.memory_record_id,
        }[self.kind]
        if required_locator is None:
            raise ValueError(f"{self.kind.value} memory source is missing its required locator")
        if (self.line_start is not None or self.line_end is not None) and self.path is None:
            raise ValueError("source line numbers require a path")
        if self.line_end is not None and self.line_start is None:
            raise ValueError("source line_end requires line_start")
        if (
            self.line_start is not None
            and self.line_end is not None
            and self.line_end < self.line_start
        ):
            raise ValueError("source line_end cannot precede line_start")
        return self


def compute_memory_content_hash(
    kind: MemoryKind,
    retrieval_text: str,
    content: dict[str, JsonValue],
) -> str:
    """Return the stable semantic payload hash used for idempotent writes."""

    payload = {
        "kind": kind.value,
        "retrieval_text": retrieval_text,
        "content": content,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MemoryRecord(VersionedMemoryModel):
    """A derived memory view with explicit lifecycle and provenance."""

    id: str = Field(default_factory=_identifier, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
    task_id: str = Field(min_length=1)
    kind: MemoryKind
    scope: MemoryScope = MemoryScope.TASK
    scope_id: str = Field(min_length=1)
    content: dict[str, JsonValue] = Field(min_length=1)
    retrieval_text: str = Field(min_length=1)
    source_ids: list[str] = Field(default_factory=list)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    status: MemoryStatus = MemoryStatus.ACTIVE
    supersedes_id: str | None = None
    superseded_by_id: str | None = None
    content_hash: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    compression_generation: int = Field(default=0, ge=0)
    derived_from_ids: list[str] = Field(default_factory=list)
    estimated_tokens: int = Field(default=0, ge=0)
    access_count: int = Field(default=0, ge=0)
    last_accessed_at: datetime | None = None
    last_recall_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if self.scope is MemoryScope.TASK and self.scope_id != self.task_id:
            raise ValueError("task-scoped memory scope_id must equal task_id")
        if self.kind is MemoryKind.WORKING and self.scope is not MemoryScope.TASK:
            raise ValueError("working memory must be task-scoped")
        if self.kind is not MemoryKind.WORKING and not self.source_ids:
            raise ValueError("long-term memory requires at least one source")
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("memory source ids must be unique")
        if self.id in {self.supersedes_id, self.superseded_by_id}:
            raise ValueError("memory cannot supersede itself")
        if self.status is MemoryStatus.SUPERSEDED and self.superseded_by_id is None:
            raise ValueError("superseded memory requires superseded_by_id")
        if self.status is not MemoryStatus.SUPERSEDED and self.superseded_by_id is not None:
            raise ValueError("only superseded memory can set superseded_by_id")
        if self.id in self.derived_from_ids:
            raise ValueError("memory cannot be derived from itself")
        if len(self.derived_from_ids) != len(set(self.derived_from_ids)):
            raise ValueError("derived memory ids must be unique")
        if self.compression_generation == 0 and self.derived_from_ids:
            raise ValueError("uncompressed memory cannot declare derived_from_ids")
        if self.compression_generation > 0 and not self.derived_from_ids:
            raise ValueError("compressed memory requires derived_from_ids")
        if self.access_count == 0 and (
            self.last_accessed_at is not None or self.last_recall_reason is not None
        ):
            raise ValueError("never-recalled memory cannot have recall metadata")
        if self.access_count > 0 and (self.last_accessed_at is None or not self.last_recall_reason):
            raise ValueError("recalled memory requires timestamp and reason")
        expected_hash = compute_memory_content_hash(self.kind, self.retrieval_text, self.content)
        if self.content_hash and self.content_hash != expected_hash:
            raise ValueError("memory content_hash does not match semantic payload")
        if not self.content_hash:
            object.__setattr__(self, "content_hash", expected_hash)
        return self


class MemoryQuery(VersionedMemoryModel):
    task_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kinds: list[MemoryKind] = Field(default_factory=_all_memory_kinds, min_length=1)
    statuses: list[MemoryStatus] = Field(
        default_factory=lambda: [MemoryStatus.ACTIVE], min_length=1
    )
    scope: MemoryScope | None = None
    scope_id: str | None = None
    paths: list[str] = Field(default_factory=list)
    step_start: int | None = Field(default=None, ge=0)
    step_end: int | None = Field(default=None, ge=0)
    created_after: datetime | None = None
    created_before: datetime | None = None
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    max_results: int = Field(default=10, ge=1, le=100)
    token_budget: int = Field(default=2_000, ge=1)
    include_ids: list[str] = Field(default_factory=list)
    exclude_ids: list[str] = Field(default_factory=list)
    error_kinds: list[ErrorKind] = Field(default_factory=list)
    plan_phases: list[str] = Field(default_factory=list)
    episode_outcomes: list[str] = Field(default_factory=list)
    fact_types: list[str] = Field(default_factory=list)
    epistemic_statuses: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_filters(self) -> Self:
        if len(self.kinds) != len(set(self.kinds)):
            raise ValueError("memory query kinds must be unique")
        if len(self.statuses) != len(set(self.statuses)):
            raise ValueError("memory query statuses must be unique")
        if len(self.paths) != len(set(self.paths)):
            raise ValueError("memory query paths must be unique")
        if len(self.include_ids) != len(set(self.include_ids)):
            raise ValueError("memory query include_ids must be unique")
        if len(self.exclude_ids) != len(set(self.exclude_ids)):
            raise ValueError("memory query exclude_ids must be unique")
        if (self.scope is None) != (self.scope_id is None):
            raise ValueError("memory query scope and scope_id must be set together")
        if (
            self.step_start is not None
            and self.step_end is not None
            and self.step_end < self.step_start
        ):
            raise ValueError("memory query step_end cannot precede step_start")
        if (
            self.created_after is not None
            and self.created_before is not None
            and self.created_before < self.created_after
        ):
            raise ValueError("memory query created_before cannot precede created_after")
        if set(self.include_ids) & set(self.exclude_ids):
            raise ValueError("memory query include_ids and exclude_ids must be disjoint")
        for label, values in (
            ("error_kinds", self.error_kinds),
            ("plan_phases", self.plan_phases),
            ("episode_outcomes", self.episode_outcomes),
            ("fact_types", self.fact_types),
            ("epistemic_statuses", self.epistemic_statuses),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"memory query {label} must be unique")
        return self


class MemoryHit(VersionedMemoryModel):
    record: MemoryRecord
    rank: int = Field(ge=1)
    score: float = Field(ge=0.0, le=1.0)
    relevance_score: float = Field(ge=0.0, le=1.0)
    recency_score: float = Field(ge=0.0, le=1.0)
    source_quality_score: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1)
    matched_terms: list[str] = Field(default_factory=list)
    estimated_tokens: int = Field(ge=0)


class MemoryBundle(VersionedMemoryModel):
    query: MemoryQuery
    hits: list[MemoryHit] = Field(default_factory=list)
    sources: list[MemorySource] = Field(default_factory=list)
    truncated: bool = False
    omitted_record_ids: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)

    @property
    def estimated_tokens(self) -> int:
        return sum(hit.estimated_tokens for hit in self.hits)

    @model_validator(mode="after")
    def validate_bundle(self) -> Self:
        record_ids = [hit.record.id for hit in self.hits]
        source_ids = [source.id for source in self.sources]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("memory bundle record ids must be unique")
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("memory bundle source ids must be unique")
        if [hit.rank for hit in self.hits] != list(range(1, len(self.hits) + 1)):
            raise ValueError("memory hit ranks must be contiguous and ordered")
        if [hit.score for hit in self.hits] != sorted(
            (hit.score for hit in self.hits), reverse=True
        ):
            raise ValueError("memory hits must be ordered by descending score")
        if any(hit.record.task_id != self.query.task_id for hit in self.hits):
            raise ValueError("memory bundle contains a record from another task")
        if any(hit.record.kind not in self.query.kinds for hit in self.hits):
            raise ValueError("memory bundle contains a kind excluded by the query")
        if any(hit.record.status not in self.query.statuses for hit in self.hits):
            raise ValueError("memory bundle contains a status excluded by the query")
        if any(hit.record.confidence < self.query.min_confidence for hit in self.hits):
            raise ValueError("memory bundle contains a record below query confidence")
        if self.query.scope is not None and any(
            hit.record.scope is not self.query.scope or hit.record.scope_id != self.query.scope_id
            for hit in self.hits
        ):
            raise ValueError("memory bundle contains a record outside query scope")
        if self.query.include_ids and any(
            hit.record.id not in self.query.include_ids for hit in self.hits
        ):
            raise ValueError("memory bundle contains a record outside query include_ids")
        if set(record_ids) & set(self.query.exclude_ids):
            raise ValueError("memory bundle contains a query-excluded record")
        if any(
            not memory_record_matches_episode_filters(hit.record, self.query) for hit in self.hits
        ):
            raise ValueError("memory bundle contains a record outside episode filters")
        if any(
            not memory_record_matches_semantic_filters(hit.record, self.query) for hit in self.hits
        ):
            raise ValueError("memory bundle contains a record outside semantic filters")
        known_sources = {source.id: source for source in self.sources}
        for hit in self.hits:
            if not set(hit.record.source_ids).issubset(known_sources):
                raise ValueError(f"memory bundle is missing sources for record {hit.record.id}")
            record_sources = [known_sources[source_id] for source_id in hit.record.source_ids]
            if self.query.paths and not any(
                source.path in self.query.paths for source in record_sources
            ):
                raise ValueError("memory bundle contains a record outside query paths")
            if (self.query.step_start is not None or self.query.step_end is not None) and not any(
                source.step_index is not None
                and (self.query.step_start is None or source.step_index >= self.query.step_start)
                and (self.query.step_end is None or source.step_index <= self.query.step_end)
                for source in record_sources
            ):
                raise ValueError("memory bundle contains a record outside query step range")
            if (
                self.query.created_after is not None
                and hit.record.created_at < self.query.created_after
            ):
                raise ValueError("memory bundle contains a record before query time range")
            if (
                self.query.created_before is not None
                and hit.record.created_at > self.query.created_before
            ):
                raise ValueError("memory bundle contains a record after query time range")
        if self.estimated_tokens > self.query.token_budget:
            raise ValueError("memory bundle exceeds query token budget")
        if len(self.hits) > self.query.max_results:
            raise ValueError("memory bundle exceeds query result limit")
        if self.truncated and not self.omitted_record_ids:
            raise ValueError("truncated memory bundle must identify omitted records")
        if not self.truncated and self.omitted_record_ids:
            raise ValueError("untruncated memory bundle cannot identify omitted records")
        if set(record_ids) & set(self.omitted_record_ids):
            raise ValueError("returned memory records cannot also be omitted")
        return self


def memory_record_matches_episode_filters(
    record: MemoryRecord,
    query: MemoryQuery,
) -> bool:
    """Return whether a record satisfies optional structured episode filters."""

    if not (query.error_kinds or query.plan_phases or query.episode_outcomes):
        return True
    if record.kind is not MemoryKind.EPISODIC:
        return False
    raw_reference = record.content.get("reference")
    if not isinstance(raw_reference, dict):
        return False
    if query.error_kinds:
        allowed_errors = {kind.value for kind in query.error_kinds}
        if raw_reference.get("error_kind") not in allowed_errors:
            return False
    if query.plan_phases and raw_reference.get("plan_phase") not in query.plan_phases:
        return False
    return not (
        query.episode_outcomes and raw_reference.get("outcome") not in query.episode_outcomes
    )


def memory_record_matches_semantic_filters(
    record: MemoryRecord,
    query: MemoryQuery,
) -> bool:
    """Return whether a record satisfies optional typed semantic filters."""

    if not (query.fact_types or query.epistemic_statuses):
        return True
    if record.kind is not MemoryKind.SEMANTIC:
        return False
    if record.content.get("semantic_schema") != "1.0":
        return False
    if query.fact_types and record.content.get("fact_type") not in query.fact_types:
        return False
    return not (
        query.epistemic_statuses
        and record.content.get("epistemic_status") not in query.epistemic_statuses
    )


class CompressionReport(VersionedMemoryModel):
    id: str = Field(default_factory=_identifier, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
    task_id: str = Field(min_length=1)
    generation: int = Field(ge=1)
    operations: list[CompressionOperation] = Field(min_length=1)
    input_record_ids: list[str] = Field(min_length=1)
    output_record_ids: list[str] = Field(min_length=1)
    protected_record_ids: list[str] = Field(default_factory=list)
    removed_record_ids: list[str] = Field(default_factory=list)
    input_tokens: int = Field(ge=1)
    output_tokens: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def saved_tokens(self) -> int:
        return self.input_tokens - self.output_tokens

    @property
    def compression_ratio(self) -> float:
        return self.output_tokens / self.input_tokens

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        for label, values in (
            ("operations", self.operations),
            ("input_record_ids", self.input_record_ids),
            ("output_record_ids", self.output_record_ids),
            ("protected_record_ids", self.protected_record_ids),
            ("removed_record_ids", self.removed_record_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"compression {label} must be unique")
        inputs = set(self.input_record_ids)
        if not set(self.protected_record_ids).issubset(inputs):
            raise ValueError("protected compression records must be inputs")
        if not set(self.removed_record_ids).issubset(inputs):
            raise ValueError("removed compression records must be inputs")
        if set(self.protected_record_ids) & set(self.removed_record_ids):
            raise ValueError("protected compression records cannot be removed")
        if not set(self.protected_record_ids).issubset(self.output_record_ids):
            raise ValueError("protected compression records must survive in outputs")
        if set(self.removed_record_ids) & set(self.output_record_ids):
            raise ValueError("removed compression records cannot remain in outputs")
        if self.output_tokens > self.input_tokens:
            raise ValueError("compression output cannot exceed input tokens")
        return self


def validate_supersession_chain(records: Sequence[MemoryRecord]) -> None:
    """Validate a complete, bidirectional, acyclic memory replacement chain."""

    by_id = {record.id: record for record in records}
    if len(by_id) != len(records):
        raise ValueError("supersession chain contains duplicate record ids")
    for record in records:
        if record.supersedes_id is not None:
            previous = by_id.get(record.supersedes_id)
            if previous is None:
                raise ValueError(f"memory {record.id} supersedes an unknown record")
            if previous.superseded_by_id != record.id:
                raise ValueError("supersession chain is not bidirectional")
            if (
                previous.task_id != record.task_id
                or previous.kind is not record.kind
                or previous.scope is not record.scope
                or previous.scope_id != record.scope_id
            ):
                raise ValueError("supersession chain crosses a memory boundary")
            if record.created_at < previous.created_at:
                raise ValueError("replacement memory cannot predate the record it supersedes")
        if record.superseded_by_id is not None:
            replacement = by_id.get(record.superseded_by_id)
            if replacement is None or replacement.supersedes_id != record.id:
                raise ValueError("supersession chain is not bidirectional")
    for origin in records:
        visited: set[str] = set()
        current: MemoryRecord | None = origin
        while current is not None:
            if current.id in visited:
                raise ValueError("supersession chain contains a cycle")
            visited.add(current.id)
            current = by_id.get(current.supersedes_id) if current.supersedes_id else None
