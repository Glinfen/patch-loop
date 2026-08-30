"""Deterministic, provenance-preserving layered memory compression."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast

from pydantic import JsonValue

from patchloop.memory.models import (
    CompressionOperation,
    CompressionReport,
    MemoryKind,
    MemoryRecord,
    MemorySource,
    MemoryStatus,
)
from patchloop.security import SecretRedactor

COMPRESSION_CONTENT_SCHEMA = "1.0"
_GENERATION_CHUNK_SIZE = 4
_EPISODE_CHUNK_SIZE = 24


class CompressionLevel(StrEnum):
    DEDUPLICATED = "deduplicated"
    RECORD_MERGE = "record_merge"
    EPISODE_AGGREGATE = "episode_aggregate"
    GENERATION_SUMMARY = "generation_summary"


@dataclass(frozen=True)
class _CompressionGroup:
    level: CompressionLevel
    records: tuple[MemoryRecord, ...]


@dataclass(frozen=True)
class CompressionBatch:
    writes: tuple[MemoryRecord, ...] = ()
    active_outputs: tuple[MemoryRecord, ...] = ()
    report: CompressionReport | None = None
    protected_record_ids: tuple[str, ...] = ()

    @property
    def compressed_record_ids(self) -> tuple[str, ...]:
        if self.report is None:
            return ()
        return tuple(self.report.removed_record_ids)

    @property
    def summary_records(self) -> tuple[MemoryRecord, ...]:
        return tuple(
            record
            for record in self.writes
            if record.compression_generation > 0 and record.status is MemoryStatus.ACTIVE
        )

    @property
    def protected_survival_rate(self) -> float:
        if not self.protected_record_ids:
            return 1.0
        active = {record.id for record in self.active_outputs}
        survived = sum(identifier in active for identifier in self.protected_record_ids)
        return survived / len(self.protected_record_ids)

    @property
    def compression_ratio(self) -> float:
        return self.report.compression_ratio if self.report is not None else 1.0


class CompressionStore(Protocol):
    def list_records(self, task_id: str) -> list[MemoryRecord]: ...

    def list_sources(self, task_id: str) -> list[MemorySource]: ...

    def save_batch(
        self,
        *,
        sources: Sequence[MemorySource] = (),
        records: Sequence[MemoryRecord] = (),
        compactions: Sequence[CompressionReport] = (),
    ) -> object: ...


class MemoryCompressor:
    """Apply safe compression without deleting source records or evidence."""

    def __init__(self, redactor: SecretRedactor | None = None) -> None:
        self.redactor = redactor or SecretRedactor()

    def compress(
        self,
        task_id: str,
        records: Sequence[MemoryRecord],
        sources: Sequence[MemorySource],
        *,
        generation_rollup: bool = False,
    ) -> CompressionBatch:
        task_records = [record for record in records if record.task_id == task_id]
        active = [record for record in task_records if record.status is MemoryStatus.ACTIVE]
        if generation_rollup:
            inputs = [record for record in active if record.compression_generation > 0]
            protected: set[str] = set()
            groups = self._generation_groups(inputs)
        else:
            inputs = active
            protected = self._protected_ids(inputs)
            groups = self._initial_groups(inputs, protected)
        if not inputs or not groups:
            return CompressionBatch(
                active_outputs=tuple(active),
                protected_record_ids=tuple(sorted(protected)),
            )

        source_map = {source.id: source for source in sources if source.task_id == task_id}
        accepted: list[tuple[_CompressionGroup, MemoryRecord]] = []
        grouped_ids: set[str] = set()
        for group in groups:
            summary = self._summary_record(task_id, group, source_map)
            input_tokens = sum(_record_tokens(record) for record in group.records)
            if summary.estimated_tokens >= input_tokens:
                continue
            accepted.append((group, summary))
            grouped_ids.update(record.id for record in group.records)
        if not accepted:
            return CompressionBatch(
                active_outputs=tuple(active),
                protected_record_ids=tuple(sorted(protected)),
            )

        invalidated = tuple(
            record.model_copy(update={"status": MemoryStatus.INVALIDATED})
            for record in inputs
            if record.id in grouped_ids
        )
        summaries = tuple(summary for _, summary in accepted)
        untouched = tuple(record for record in inputs if record.id not in grouped_ids)
        report_outputs = (*untouched, *summaries)
        input_id_set = {record.id for record in inputs}
        outside_inputs = tuple(record for record in active if record.id not in input_id_set)
        active_outputs = (*outside_inputs, *report_outputs)
        input_tokens = sum(_record_tokens(record) for record in inputs)
        output_tokens = sum(_record_tokens(record) for record in report_outputs)
        operations = self._operations(accepted)
        input_ids = sorted(record.id for record in inputs)
        output_ids = sorted(record.id for record in report_outputs)
        removed_ids = sorted(grouped_ids)
        generation = max(summary.compression_generation for summary in summaries)
        created_at = max(record.created_at for record in inputs)
        report_id = _stable_id(
            "compression",
            task_id,
            str(generation),
            _hash_payload(
                {
                    "inputs": input_ids,
                    "outputs": output_ids,
                    "operations": [operation.value for operation in operations],
                }
            ),
        )
        report = CompressionReport(
            id=report_id,
            task_id=task_id,
            generation=generation,
            operations=operations,
            input_record_ids=input_ids,
            output_record_ids=output_ids,
            protected_record_ids=sorted(protected & set(input_ids)),
            removed_record_ids=removed_ids,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            created_at=created_at,
        )
        return CompressionBatch(
            writes=(*invalidated, *summaries),
            active_outputs=active_outputs,
            report=report,
            protected_record_ids=tuple(sorted(protected)),
        )

    def compress_store(
        self,
        store: CompressionStore,
        task_id: str,
        *,
        generation_rollup: bool = False,
    ) -> CompressionBatch:
        batch = self.compress(
            task_id,
            store.list_records(task_id),
            store.list_sources(task_id),
            generation_rollup=generation_rollup,
        )
        if batch.report is not None:
            store.save_batch(records=batch.writes, compactions=[batch.report])
        return batch

    def _initial_groups(
        self,
        records: Sequence[MemoryRecord],
        protected: set[str],
    ) -> list[_CompressionGroup]:
        eligible = [
            record
            for record in records
            if record.id not in protected and record.compression_generation == 0
        ]
        assigned: set[str] = set()
        groups: list[_CompressionGroup] = []

        duplicate_buckets: dict[tuple[str, str, str, str], list[MemoryRecord]] = defaultdict(list)
        for record in eligible:
            duplicate_buckets[
                (
                    record.kind.value,
                    record.scope.value,
                    record.scope_id,
                    _normalize(record.retrieval_text),
                )
            ].append(record)
        for bucket in duplicate_buckets.values():
            if len(bucket) < 2:
                continue
            ordered = tuple(sorted(bucket, key=_record_order))
            groups.append(_CompressionGroup(CompressionLevel.DEDUPLICATED, ordered))
            assigned.update(record.id for record in ordered)

        semantic_buckets: dict[tuple[str, str, str], list[MemoryRecord]] = defaultdict(list)
        for record in eligible:
            if record.id in assigned or record.kind is not MemoryKind.SEMANTIC:
                continue
            key = _semantic_merge_key(record)
            if key is not None:
                semantic_buckets[key].append(record)
        for bucket in semantic_buckets.values():
            if len(bucket) < 2:
                continue
            ordered = tuple(sorted(bucket, key=_record_order))
            groups.append(_CompressionGroup(CompressionLevel.RECORD_MERGE, ordered))
            assigned.update(record.id for record in ordered)

        episode_buckets: dict[tuple[str, str, str, str], list[MemoryRecord]] = defaultdict(list)
        for record in eligible:
            if record.id in assigned or record.kind is not MemoryKind.EPISODIC:
                continue
            episode_buckets[_episode_group_key(record)].append(record)
        for bucket in episode_buckets.values():
            ordered_episodes = sorted(bucket, key=_record_order)
            for chunk in _chunks(ordered_episodes, _EPISODE_CHUNK_SIZE):
                if len(chunk) < 2:
                    continue
                group_records = tuple(chunk)
                groups.append(_CompressionGroup(CompressionLevel.EPISODE_AGGREGATE, group_records))
                assigned.update(record.id for record in group_records)
        return groups

    @staticmethod
    def _generation_groups(records: Sequence[MemoryRecord]) -> list[_CompressionGroup]:
        buckets: dict[tuple[str, str, str], list[MemoryRecord]] = defaultdict(list)
        for record in records:
            buckets[(record.kind.value, record.scope.value, record.scope_id)].append(record)
        groups: list[_CompressionGroup] = []
        for bucket in buckets.values():
            ordered = sorted(bucket, key=_record_order)
            for chunk in _chunks(ordered, _GENERATION_CHUNK_SIZE):
                if len(chunk) >= 2:
                    groups.append(
                        _CompressionGroup(
                            CompressionLevel.GENERATION_SUMMARY,
                            tuple(chunk),
                        )
                    )
        return groups

    def _protected_ids(self, records: Sequence[MemoryRecord]) -> set[str]:
        protected = {record.id for record in records if _always_protected(record)}
        latest_verified: dict[tuple[str, tuple[str, ...]], MemoryRecord] = {}
        latest_recovery: dict[tuple[str, ...], MemoryRecord] = {}
        for record in records:
            if record.kind is not MemoryKind.EPISODIC:
                continue
            outcome = _episode_outcome(record)
            paths = tuple(_content_paths(record))
            if outcome == "verified":
                key = (_episode_tool(record), paths)
                current = latest_verified.get(key)
                if current is None or _record_order(record) > _record_order(current):
                    latest_verified[key] = record
            if outcome == "recovered":
                current = latest_recovery.get(paths)
                if current is None or _record_order(record) > _record_order(current):
                    latest_recovery[paths] = record
        protected.update(record.id for record in latest_verified.values())
        protected.update(record.id for record in latest_recovery.values())
        return protected

    def _summary_record(
        self,
        task_id: str,
        group: _CompressionGroup,
        sources: Mapping[str, MemorySource],
    ) -> MemoryRecord:
        records = list(group.records)
        direct_ids = sorted(record.id for record in records)
        lineage_ids = sorted(
            {identifier for record in records for identifier in _record_lineage(record)}
        )
        source_ids = sorted({source_id for record in records for source_id in record.source_ids})
        source_steps = sorted(
            {
                source.step_index
                for source_id in source_ids
                if (source := sources.get(source_id)) is not None and source.step_index is not None
            }
        )
        summary = self.redactor.redact_text(self._summary_text(group))
        generation = max(record.compression_generation for record in records) + 1
        content: dict[str, JsonValue] = {
            "compression_schema": COMPRESSION_CONTENT_SCHEMA,
            "level": group.level.value,
            "summary": summary,
            "input_record_ids": cast(list[JsonValue], direct_ids),
            "lineage_record_ids": cast(list[JsonValue], lineage_ids),
            "source_steps": cast(list[JsonValue], source_steps),
            "untrusted": True,
            "provenance_hash": _hash_payload(
                {"records": direct_ids, "sources": source_ids, "level": group.level.value}
            ),
        }
        retrieval = f"Compressed {group.level.value}: {summary}"
        identifier = _stable_id(
            "compressed-memory",
            task_id,
            group.level.value,
            str(generation),
            _hash_payload({"records": direct_ids, "content": content}),
        )
        return MemoryRecord(
            id=identifier,
            task_id=task_id,
            kind=records[0].kind,
            scope=records[0].scope,
            scope_id=records[0].scope_id,
            content=content,
            retrieval_text=retrieval,
            source_ids=source_ids,
            importance=max(record.importance for record in records),
            confidence=min(record.confidence for record in records),
            compression_generation=generation,
            derived_from_ids=direct_ids,
            estimated_tokens=_estimate_text(retrieval),
            created_at=max(record.created_at for record in records),
        )

    @staticmethod
    def _summary_text(group: _CompressionGroup) -> str:
        records = list(group.records)
        if group.level is CompressionLevel.DEDUPLICATED:
            return f"{_bounded(records[-1].retrieval_text, 480)}; repeated={len(records)}"
        if group.level is CompressionLevel.RECORD_MERGE:
            unique = _unique_texts(record.retrieval_text for record in records)
            return f"evidence_count={len(records)}; " + " | ".join(
                _bounded(value, 220) for value in unique[:3]
            )
        if group.level is CompressionLevel.EPISODE_AGGREGATE:
            steps = sorted(
                step for record in records if (step := _episode_step(record)) is not None
            )
            paths = sorted({path for record in records for path in _content_paths(record)})
            outcomes = sorted({_episode_outcome(record) for record in records})
            first = _bounded(records[0].retrieval_text, 180)
            final = _bounded(records[-1].retrieval_text, 220)
            step_range = f"{steps[0]}-{steps[-1]}" if steps else "unknown"
            return (
                f"episodes={len(records)}; steps={step_range}; "
                f"paths={','.join(paths) or 'none'}; outcomes={','.join(outcomes)}; "
                f"first={first}; final={final}"
            )
        summaries = _unique_texts(
            str(record.content.get("summary") or record.retrieval_text) for record in records
        )
        return f"generation_inputs={len(records)}; " + " | ".join(
            _bounded(value, 180) for value in summaries
        )

    @staticmethod
    def _operations(
        accepted: Sequence[tuple[_CompressionGroup, MemoryRecord]],
    ) -> list[CompressionOperation]:
        levels = {group.level for group, _ in accepted}
        operations: list[CompressionOperation] = []
        if CompressionLevel.DEDUPLICATED in levels:
            operations.append(CompressionOperation.DEDUPLICATE)
        if CompressionLevel.RECORD_MERGE in levels:
            operations.append(CompressionOperation.MERGE)
        if levels & {
            CompressionLevel.EPISODE_AGGREGATE,
            CompressionLevel.GENERATION_SUMMARY,
        }:
            operations.append(CompressionOperation.SUMMARIZE)
        operations.append(CompressionOperation.PRUNE)
        return operations


def _always_protected(record: MemoryRecord) -> bool:
    content = record.content
    fact_type = content.get("fact_type")
    epistemic = content.get("epistemic_status")
    if fact_type in {"constraint", "prohibition", "verification"}:
        return True
    if epistemic == "user_asserted":
        return True
    if "verification_tool" in content or content.get("final_decision") is True:
        return True
    status = content.get("plan_status") or content.get("status")
    if status in {"pending", "running"}:
        return True
    if record.kind is MemoryKind.EPISODIC and _episode_outcome(record) == "failed":
        return True
    return bool(record.importance >= 0.99 and record.confidence >= 0.99)


def _semantic_merge_key(record: MemoryRecord) -> tuple[str, str, str] | None:
    content = record.content
    slot = content.get("slot_key")
    value = content.get("normalized_value")
    if isinstance(slot, str) and isinstance(value, str):
        return record.scope_id, slot, value
    fact = content.get("fact")
    if isinstance(fact, str):
        return record.scope_id, "fact", _normalize(fact)
    tool = content.get("verification_tool")
    result = content.get("result")
    if isinstance(tool, str) and isinstance(result, str):
        return record.scope_id, tool, _normalize(result)
    return None


def _episode_group_key(record: MemoryRecord) -> tuple[str, str, str, str]:
    content = record.content
    phase = str(content.get("plan_phase") or content.get("intent") or "unknown")
    paths = ",".join(_content_paths(record))
    return record.scope_id, phase, paths, _episode_tool(record)


def _episode_outcome(record: MemoryRecord) -> str:
    raw = record.content.get("outcome")
    if isinstance(raw, str):
        return raw
    reference = record.content.get("reference")
    if isinstance(reference, dict):
        outcome = reference.get("outcome")
        if isinstance(outcome, str):
            return outcome
    return "unknown"


def _episode_tool(record: MemoryRecord) -> str:
    reference = record.content.get("reference")
    if isinstance(reference, dict):
        tool_name = reference.get("tool_name")
        if isinstance(tool_name, str):
            return tool_name
    actions = record.content.get("actions")
    if isinstance(actions, list) and actions and isinstance(actions[0], dict):
        raw = actions[0].get("tool_name")
        if isinstance(raw, str):
            return raw
    return "unknown"


def _episode_step(record: MemoryRecord) -> int | None:
    raw = record.content.get("step_index")
    if isinstance(raw, int):
        return raw
    reference = record.content.get("reference")
    if isinstance(reference, dict):
        step_index = reference.get("step_index")
        if isinstance(step_index, int):
            return step_index
    return None


def _content_paths(record: MemoryRecord) -> list[str]:
    raw = record.content.get("paths")
    if isinstance(raw, list):
        return sorted({path for path in raw if isinstance(path, str)})
    reference = record.content.get("reference")
    if isinstance(reference, dict):
        paths = reference.get("paths")
        if isinstance(paths, list):
            return sorted({path for path in paths if isinstance(path, str)})
    return []


def _record_tokens(record: MemoryRecord) -> int:
    return max(1, record.estimated_tokens or _estimate_text(record.retrieval_text))


def _record_order(record: MemoryRecord) -> tuple[datetime, str]:
    value = record.created_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC), record.id


def _chunks(records: Sequence[MemoryRecord], size: int) -> list[list[MemoryRecord]]:
    return [list(records[index : index + size]) for index in range(0, len(records), size)]


def _record_lineage(record: MemoryRecord) -> list[str]:
    lineage = [record.id, *record.derived_from_ids]
    raw = record.content.get("lineage_record_ids")
    if isinstance(raw, list):
        lineage.extend(identifier for identifier in raw if isinstance(identifier, str))
    return lineage


def _unique_texts(values: Iterable[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for raw in values:
        normalized = _normalize(raw)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(" ".join(raw.split()))
    return unique


def _bounded(value: str, max_chars: int) -> str:
    compact = " ".join(value.split())
    if len(compact) <= max_chars:
        return compact
    suffix = " ... [compressed]"
    return compact[: max_chars - len(suffix)] + suffix


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _estimate_text(value: str) -> int:
    return math.ceil(len(value.encode("utf-8")) / 3) + 4


def _hash_payload(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256(":".join(values).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"
