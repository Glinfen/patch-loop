"""Transactional SQLite persistence and retrieval for layered memory."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from patchloop.memory.models import (
    CompressionReport,
    MemoryBundle,
    MemoryHit,
    MemoryQuery,
    MemoryRecord,
    MemorySource,
    MemorySourceKind,
    memory_record_matches_episode_filters,
    memory_record_matches_semantic_filters,
)
from patchloop.security import SecretRedactor
from patchloop.sqlite_support import connect, connect_write

MEMORY_STORE_SCHEMA_VERSION = 1
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_./:-]+|[\u4e00-\u9fff]+")
_MEMORY_TABLES = {
    "memory_sources",
    "memory_records",
    "memory_record_sources",
    "memory_compactions",
}


class MemoryStoreError(RuntimeError):
    pass


class MemoryStoreConflictError(MemoryStoreError):
    pass


class MemoryVectorIndex(Protocol):
    """Optional in-process semantic scorer; no network backend is required."""

    def score(self, query: str, records: Sequence[MemoryRecord]) -> Mapping[str, float]: ...


class MemoryLeaseGuard(Protocol):
    @property
    def execution_id(self) -> str: ...

    @property
    def task_id(self) -> str: ...

    @property
    def token(self) -> str: ...

    @property
    def generation(self) -> int: ...

    @property
    def owner_id(self) -> str: ...


@dataclass(frozen=True)
class MemoryWriteResult:
    sources: tuple[MemorySource, ...]
    records: tuple[MemoryRecord, ...]
    compactions: tuple[CompressionReport, ...]


_MIGRATION_V1 = (
    """
    CREATE TABLE memory_sources (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL,
        step_index INTEGER,
        path TEXT,
        payload_json TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        UNIQUE (task_id, source_fingerprint),
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE memory_records (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        scope TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        identity_hash TEXT NOT NULL,
        importance REAL NOT NULL,
        confidence REAL NOT NULL,
        compression_generation INTEGER NOT NULL,
        estimated_tokens INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        UNIQUE (task_id, identity_hash),
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE memory_record_sources (
        task_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        PRIMARY KEY (record_id, source_id),
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
        FOREIGN KEY (record_id) REFERENCES memory_records(id) ON DELETE CASCADE,
        FOREIGN KEY (source_id) REFERENCES memory_sources(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE memory_compactions (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        report_fingerprint TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (task_id, report_fingerprint),
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX idx_memory_sources_task_step ON memory_sources(task_id, step_index)",
    "CREATE INDEX idx_memory_sources_task_path ON memory_sources(task_id, path)",
    "CREATE INDEX idx_memory_records_task_kind_status ON memory_records(task_id, kind, status)",
    "CREATE INDEX idx_memory_records_task_content_hash ON memory_records(task_id, content_hash)",
    "CREATE INDEX idx_memory_records_task_scope ON memory_records(task_id, scope, scope_id)",
    "CREATE INDEX idx_memory_records_task_created ON memory_records(task_id, created_at)",
    "CREATE INDEX idx_memory_record_sources_source ON memory_record_sources(source_id, record_id)",
    """
    CREATE INDEX idx_memory_compactions_task_generation
    ON memory_compactions(task_id, generation)
    """,
)


def initialize_memory_schema(connection: sqlite3.Connection) -> None:
    """Apply memory migrations atomically without rewriting runtime tables."""

    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).fetchone()
        is None
    ):
        raise MemoryStoreError("memory store requires an initialized PatchLoop tasks table")
    connection.execute("SAVEPOINT patchloop_memory_migration")
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS patchloop_schema_migrations (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        row = connection.execute(
            "SELECT version FROM patchloop_schema_migrations WHERE component = 'memory'"
        ).fetchone()
        version = 0 if row is None else int(row[0])
        if version > MEMORY_STORE_SCHEMA_VERSION:
            raise MemoryStoreError(
                f"memory schema {version} is newer than supported {MEMORY_STORE_SCHEMA_VERSION}"
            )
        if version < 1:
            for statement in _MIGRATION_V1:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO patchloop_schema_migrations (component, version, updated_at)
                VALUES ('memory', ?, ?)
                """,
                (MEMORY_STORE_SCHEMA_VERSION, datetime.now(UTC).isoformat()),
            )
        tables = {
            str(table[0])
            for table in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not _MEMORY_TABLES.issubset(tables):
            missing = ", ".join(sorted(_MEMORY_TABLES - tables))
            raise MemoryStoreError(f"memory schema is incomplete; missing: {missing}")
        connection.execute("RELEASE SAVEPOINT patchloop_memory_migration")
    except Exception as exc:
        connection.execute("ROLLBACK TO SAVEPOINT patchloop_memory_migration")
        connection.execute("RELEASE SAVEPOINT patchloop_memory_migration")
        if isinstance(exc, MemoryStoreError):
            raise
        raise MemoryStoreError(f"memory schema migration failed: {exc}") from exc


class SQLiteMemoryStore:
    def __init__(
        self,
        path: Path,
        redactor: SecretRedactor | None = None,
        vector_index: MemoryVectorIndex | None = None,
    ) -> None:
        self.path = path
        self.redactor = redactor or SecretRedactor()
        self.vector_index = vector_index
        if not self.path.is_file():
            raise MemoryStoreError("memory store requires an existing PatchLoop database")
        with connect(self.path) as connection:
            initialize_memory_schema(connection)

    def save_source(
        self, source: MemorySource, *, lease_guard: MemoryLeaseGuard | None = None
    ) -> MemorySource:
        return self.save_batch(sources=[source], lease_guard=lease_guard).sources[0]

    def save_record(
        self, record: MemoryRecord, *, lease_guard: MemoryLeaseGuard | None = None
    ) -> MemoryRecord:
        return self.save_batch(records=[record], lease_guard=lease_guard).records[0]

    def save_compaction(
        self, report: CompressionReport, *, lease_guard: MemoryLeaseGuard | None = None
    ) -> CompressionReport:
        return self.save_batch(compactions=[report], lease_guard=lease_guard).compactions[0]

    def save_batch(
        self,
        *,
        sources: Sequence[MemorySource] = (),
        records: Sequence[MemoryRecord] = (),
        compactions: Sequence[CompressionReport] = (),
        lease_guard: MemoryLeaseGuard | None = None,
    ) -> MemoryWriteResult:
        """Persist one recovery-safe batch; any failed item rolls back every write."""

        safe_sources = [self._redact_source(source) for source in sources]
        safe_records = [self._redact_record(record) for record in records]
        safe_compactions = [self._redact_compaction(report) for report in compactions]
        task_ids = {
            *(source.task_id for source in safe_sources),
            *(record.task_id for record in safe_records),
            *(report.task_id for report in safe_compactions),
        }
        if len(task_ids) > 1:
            raise MemoryStoreError("one memory batch must belong to one task")
        with connect_write(self.path) as connection:
            if task_ids:
                self._assert_memory_guard(connection, next(iter(task_ids)), lease_guard)
            source_id_map: dict[str, str] = {}
            persisted_sources: list[MemorySource] = []
            for source in safe_sources:
                persisted = self._write_source(connection, source)
                source_id_map[source.id] = persisted.id
                persisted_sources.append(persisted)
            record_id_map: dict[str, str] = {}
            persisted_records: list[MemoryRecord] = []
            for record in safe_records:
                canonical = self._canonicalize_record(record, source_id_map, record_id_map)
                persisted_record = self._write_record(connection, canonical)
                record_id_map[record.id] = persisted_record.id
                persisted_records.append(persisted_record)
            persisted_compactions: list[CompressionReport] = []
            for report in safe_compactions:
                canonical_report = self._canonicalize_compaction(report, record_id_map)
                persisted_compactions.append(self._write_compaction(connection, canonical_report))
        return MemoryWriteResult(
            sources=tuple(persisted_sources),
            records=tuple(persisted_records),
            compactions=tuple(persisted_compactions),
        )

    def restore_checkpoint_state(
        self,
        *,
        sources: Sequence[MemorySource] = (),
        records: Sequence[MemoryRecord] = (),
        compactions: Sequence[CompressionReport] = (),
        lease_guard: MemoryLeaseGuard | None = None,
    ) -> MemoryWriteResult:
        """Replay derived checkpoint memory through the same idempotent transaction."""

        return self.save_batch(
            sources=sources,
            records=records,
            compactions=compactions,
            lease_guard=lease_guard,
        )

    @staticmethod
    def _assert_memory_guard(
        connection: sqlite3.Connection,
        task_id: str,
        lease_guard: MemoryLeaseGuard | None,
    ) -> None:
        task = connection.execute(
            "SELECT session_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            raise MemoryStoreError(f"memory task not found: {task_id}")
        if task["session_id"] is None:
            return
        if lease_guard is None or lease_guard.task_id != task_id:
            from patchloop.persistence_contracts import LeaseLost

            raise LeaseLost(task_id)
        row = connection.execute(
            "SELECT * FROM executions WHERE id = ?", (lease_guard.execution_id,)
        ).fetchone()
        if (
            row is None
            or str(row["task_id"]) != task_id
            or str(row["owner_id"]) != lease_guard.owner_id
            or str(row["lease_token"]) != lease_guard.token
            or int(row["generation"]) != lease_guard.generation
            or str(row["status"]) not in {"claimed", "running", "waiting_for_approval", "paused"}
            or datetime.fromisoformat(str(row["lease_expires_at"])) <= datetime.now(UTC)
        ):
            from patchloop.persistence_contracts import LeaseLost

            raise LeaseLost(task_id)

    def get_source(self, source_id: str) -> MemorySource:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM memory_sources WHERE id = ?", (source_id,)
            ).fetchone()
        if row is None:
            raise MemoryStoreError(f"memory source not found: {source_id}")
        return MemorySource.model_validate_json(row["payload_json"])

    def get_record(self, record_id: str) -> MemoryRecord:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM memory_records WHERE id = ?", (record_id,)
            ).fetchone()
        if row is None:
            raise MemoryStoreError(f"memory record not found: {record_id}")
        return MemoryRecord.model_validate_json(row["payload_json"])

    def list_sources(self, task_id: str) -> list[MemorySource]:
        with connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM memory_sources
                WHERE task_id = ? ORDER BY captured_at, id
                """,
                (task_id,),
            ).fetchall()
        return [MemorySource.model_validate_json(row["payload_json"]) for row in rows]

    def list_records(self, task_id: str) -> list[MemoryRecord]:
        with connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM memory_records
                WHERE task_id = ? ORDER BY created_at, id
                """,
                (task_id,),
            ).fetchall()
        return [MemoryRecord.model_validate_json(row["payload_json"]) for row in rows]

    def list_compactions(self, task_id: str) -> list[CompressionReport]:
        with connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM memory_compactions
                WHERE task_id = ? ORDER BY generation, created_at, id
                """,
                (task_id,),
            ).fetchall()
        return [CompressionReport.model_validate_json(row["payload_json"]) for row in rows]

    def find_records_by_content_hash(self, task_id: str, content_hash: str) -> list[MemoryRecord]:
        with connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM memory_records
                WHERE task_id = ? AND content_hash = ? ORDER BY created_at, id
                """,
                (task_id, content_hash),
            ).fetchall()
        return [MemoryRecord.model_validate_json(row["payload_json"]) for row in rows]

    def query(self, query: MemoryQuery) -> MemoryBundle:
        safe_query = self._redact_query(query)
        with connect(self.path) as connection:
            records = self._select_records(connection, safe_query)
            records = [
                record
                for record in records
                if memory_record_matches_episode_filters(record, safe_query)
                and memory_record_matches_semantic_filters(record, safe_query)
            ]
            source_map = self._sources_for_records(connection, records)
        relevance = self._relevance_scores(safe_query.text, records)
        ranked = self._rank_records(records, source_map, relevance)
        hits: list[MemoryHit] = []
        omitted: list[str] = []
        used_tokens = 0
        for record, score, relevance_score, recency_score, source_quality in ranked:
            tokens = record.estimated_tokens
            if (
                len(hits) >= safe_query.max_results
                or used_tokens + tokens > safe_query.token_budget
            ):
                omitted.append(record.id)
                continue
            hits.append(
                MemoryHit(
                    record=record,
                    rank=len(hits) + 1,
                    score=score,
                    relevance_score=relevance_score,
                    recency_score=recency_score,
                    source_quality_score=source_quality,
                    reason=self._retrieval_reason(record, relevance_score, source_quality),
                    matched_terms=sorted(_tokens(safe_query.text) & _tokens(record.retrieval_text)),
                    estimated_tokens=tokens,
                )
            )
            used_tokens += tokens
        selected_source_ids = {source_id for hit in hits for source_id in hit.record.source_ids}
        selected_sources = sorted(
            (source_map[source_id] for source_id in selected_source_ids),
            key=lambda item: item.id,
        )
        return MemoryBundle(
            query=safe_query,
            hits=hits,
            sources=selected_sources,
            truncated=bool(omitted),
            omitted_record_ids=omitted,
        )

    def _redact_source(self, source: MemorySource) -> MemorySource:
        payload = self.redactor.redact(source.model_dump(mode="json"))
        return MemorySource.model_validate(payload)

    def _redact_record(self, record: MemoryRecord) -> MemoryRecord:
        payload = self.redactor.redact(record.model_dump(mode="json"))
        payload["content_hash"] = ""
        return MemoryRecord.model_validate(payload)

    def _redact_compaction(self, report: CompressionReport) -> CompressionReport:
        payload = self.redactor.redact(report.model_dump(mode="json"))
        return CompressionReport.model_validate(payload)

    def _redact_query(self, query: MemoryQuery) -> MemoryQuery:
        payload = self.redactor.redact(query.model_dump(mode="json"))
        return MemoryQuery.model_validate(payload)

    @staticmethod
    def _canonicalize_record(
        record: MemoryRecord,
        source_id_map: Mapping[str, str],
        record_id_map: Mapping[str, str],
    ) -> MemoryRecord:
        payload = record.model_dump(mode="json")
        payload["source_ids"] = [source_id_map.get(item, item) for item in record.source_ids]
        payload["derived_from_ids"] = [
            record_id_map.get(item, item) for item in record.derived_from_ids
        ]
        if record.supersedes_id is not None:
            payload["supersedes_id"] = record_id_map.get(record.supersedes_id, record.supersedes_id)
        if record.superseded_by_id is not None:
            payload["superseded_by_id"] = record_id_map.get(
                record.superseded_by_id, record.superseded_by_id
            )
        return MemoryRecord.model_validate(payload)

    @staticmethod
    def _canonicalize_compaction(
        report: CompressionReport, record_id_map: Mapping[str, str]
    ) -> CompressionReport:
        payload = report.model_dump(mode="json")
        for field in (
            "input_record_ids",
            "output_record_ids",
            "protected_record_ids",
            "removed_record_ids",
        ):
            payload[field] = [record_id_map.get(item, item) for item in payload[field]]
        return CompressionReport.model_validate(payload)

    def _write_source(self, connection: sqlite3.Connection, source: MemorySource) -> MemorySource:
        fingerprint = _source_fingerprint(source)
        row = connection.execute(
            """
            SELECT id, payload_json FROM memory_sources
            WHERE task_id = ? AND source_fingerprint = ?
            """,
            (source.task_id, fingerprint),
        ).fetchone()
        if row is not None:
            return MemorySource.model_validate_json(row["payload_json"])
        collision = connection.execute(
            "SELECT source_fingerprint FROM memory_sources WHERE id = ?", (source.id,)
        ).fetchone()
        if collision is not None:
            raise MemoryStoreConflictError(f"memory source id collision: {source.id}")
        try:
            connection.execute(
                """
                INSERT INTO memory_sources (
                    id, task_id, kind, evidence_hash, source_fingerprint,
                    step_index, path, payload_json, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.id,
                    source.task_id,
                    source.kind,
                    source.evidence_hash,
                    fingerprint,
                    source.step_index,
                    source.path,
                    _model_json(source),
                    _utc_iso(source.captured_at),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise MemoryStoreError(f"cannot persist memory source {source.id}: {exc}") from exc
        return source

    def _write_record(self, connection: sqlite3.Connection, record: MemoryRecord) -> MemoryRecord:
        self._require_sources(connection, record)
        identity = _record_identity(record)
        row = connection.execute(
            """
            SELECT id, payload_json FROM memory_records
            WHERE task_id = ? AND identity_hash = ?
            """,
            (record.task_id, identity),
        ).fetchone()
        if row is not None and row["id"] != record.id:
            return MemoryRecord.model_validate_json(row["payload_json"])
        collision = connection.execute(
            "SELECT identity_hash FROM memory_records WHERE id = ?", (record.id,)
        ).fetchone()
        if collision is not None and collision["identity_hash"] != identity:
            raise MemoryStoreConflictError(f"memory record id collision: {record.id}")
        values = (
            record.task_id,
            record.kind,
            record.status,
            record.scope,
            record.scope_id,
            record.content_hash,
            identity,
            record.importance,
            record.confidence,
            record.compression_generation,
            record.estimated_tokens,
            _utc_iso(record.created_at),
            _model_json(record),
            record.id,
        )
        try:
            if collision is None:
                connection.execute(
                    """
                    INSERT INTO memory_records (
                        task_id, kind, status, scope, scope_id, content_hash,
                        identity_hash, importance, confidence, compression_generation,
                        estimated_tokens, created_at, payload_json, id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            else:
                connection.execute(
                    """
                    UPDATE memory_records SET
                        task_id = ?, kind = ?, status = ?, scope = ?, scope_id = ?,
                        content_hash = ?, identity_hash = ?, importance = ?, confidence = ?,
                        compression_generation = ?, estimated_tokens = ?, created_at = ?,
                        payload_json = ? WHERE id = ?
                    """,
                    values,
                )
                connection.execute(
                    "DELETE FROM memory_record_sources WHERE record_id = ?", (record.id,)
                )
            connection.executemany(
                """
                INSERT INTO memory_record_sources (task_id, record_id, source_id)
                VALUES (?, ?, ?)
                """,
                [(record.task_id, record.id, source_id) for source_id in record.source_ids],
            )
        except sqlite3.IntegrityError as exc:
            raise MemoryStoreError(f"cannot persist memory record {record.id}: {exc}") from exc
        return record

    def _write_compaction(
        self, connection: sqlite3.Connection, report: CompressionReport
    ) -> CompressionReport:
        self._require_compaction_records(connection, report)
        fingerprint = _compaction_fingerprint(report)
        row = connection.execute(
            """
            SELECT id, payload_json FROM memory_compactions
            WHERE task_id = ? AND report_fingerprint = ?
            """,
            (report.task_id, fingerprint),
        ).fetchone()
        if row is not None:
            return CompressionReport.model_validate_json(row["payload_json"])
        collision = connection.execute(
            "SELECT report_fingerprint FROM memory_compactions WHERE id = ?", (report.id,)
        ).fetchone()
        if collision is not None:
            raise MemoryStoreConflictError(f"memory compaction id collision: {report.id}")
        try:
            connection.execute(
                """
                INSERT INTO memory_compactions (
                    id, task_id, generation, report_fingerprint, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    report.id,
                    report.task_id,
                    report.generation,
                    fingerprint,
                    _model_json(report),
                    _utc_iso(report.created_at),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise MemoryStoreError(f"cannot persist memory compaction {report.id}: {exc}") from exc
        return report

    @staticmethod
    def _require_sources(connection: sqlite3.Connection, record: MemoryRecord) -> None:
        if not record.source_ids:
            return
        placeholders = ",".join("?" for _ in record.source_ids)
        rows = connection.execute(
            f"""
            SELECT id, task_id FROM memory_sources
            WHERE id IN ({placeholders})
            """,
            tuple(record.source_ids),
        ).fetchall()
        found = {row["id"]: row["task_id"] for row in rows}
        missing = sorted(set(record.source_ids) - set(found))
        if missing:
            raise MemoryStoreError(
                f"memory record references unknown sources: {', '.join(missing)}"
            )
        if any(task_id != record.task_id for task_id in found.values()):
            raise MemoryStoreError("memory record cannot reference another task's source")

    @staticmethod
    def _require_compaction_records(
        connection: sqlite3.Connection, report: CompressionReport
    ) -> None:
        identifiers = set(report.input_record_ids) | set(report.output_record_ids)
        placeholders = ",".join("?" for _ in identifiers)
        rows = connection.execute(
            f"SELECT id, task_id FROM memory_records WHERE id IN ({placeholders})",
            tuple(sorted(identifiers)),
        ).fetchall()
        found = {row["id"]: row["task_id"] for row in rows}
        missing = sorted(identifiers - set(found))
        if missing:
            raise MemoryStoreError(
                f"memory compaction references unknown records: {', '.join(missing)}"
            )
        if any(task_id != report.task_id for task_id in found.values()):
            raise MemoryStoreError("memory compaction cannot reference another task's record")

    @staticmethod
    def _select_records(connection: sqlite3.Connection, query: MemoryQuery) -> list[MemoryRecord]:
        clauses = ["r.task_id = ?"]
        parameters: list[object] = [query.task_id]
        _append_in_filter(clauses, parameters, "r.kind", [kind.value for kind in query.kinds])
        _append_in_filter(
            clauses, parameters, "r.status", [status.value for status in query.statuses]
        )
        clauses.append("r.confidence >= ?")
        parameters.append(query.min_confidence)
        if query.scope is not None:
            clauses.extend(["r.scope = ?", "r.scope_id = ?"])
            parameters.extend([query.scope.value, query.scope_id])
        if query.include_ids:
            _append_in_filter(clauses, parameters, "r.id", query.include_ids)
        if query.exclude_ids:
            placeholders = ",".join("?" for _ in query.exclude_ids)
            clauses.append(f"r.id NOT IN ({placeholders})")
            parameters.extend(query.exclude_ids)
        if query.created_after is not None:
            clauses.append("r.created_at >= ?")
            parameters.append(_utc_iso(query.created_after))
        if query.created_before is not None:
            clauses.append("r.created_at <= ?")
            parameters.append(_utc_iso(query.created_before))
        source_clauses: list[str] = []
        if query.paths:
            placeholders = ",".join("?" for _ in query.paths)
            source_clauses.append(f"s.path IN ({placeholders})")
            parameters.extend(query.paths)
        if query.step_start is not None:
            source_clauses.append("s.step_index >= ?")
            parameters.append(query.step_start)
        if query.step_end is not None:
            source_clauses.append("s.step_index <= ?")
            parameters.append(query.step_end)
        if source_clauses:
            clauses.append(
                """
                EXISTS (
                    SELECT 1 FROM memory_record_sources mrs
                    JOIN memory_sources s ON s.id = mrs.source_id
                    WHERE mrs.record_id = r.id AND
                """
                + " AND ".join(source_clauses)
                + ")"
            )
        rows = connection.execute(
            "SELECT r.payload_json FROM memory_records r WHERE "
            + " AND ".join(clauses)
            + " ORDER BY r.created_at, r.id",
            parameters,
        ).fetchall()
        return [MemoryRecord.model_validate_json(row["payload_json"]) for row in rows]

    @staticmethod
    def _sources_for_records(
        connection: sqlite3.Connection, records: Sequence[MemoryRecord]
    ) -> dict[str, MemorySource]:
        identifiers = sorted({source_id for record in records for source_id in record.source_ids})
        if not identifiers:
            return {}
        placeholders = ",".join("?" for _ in identifiers)
        rows = connection.execute(
            f"SELECT payload_json FROM memory_sources WHERE id IN ({placeholders})",
            identifiers,
        ).fetchall()
        return {
            source.id: source
            for source in (MemorySource.model_validate_json(row["payload_json"]) for row in rows)
        }

    def _relevance_scores(self, query: str, records: Sequence[MemoryRecord]) -> dict[str, float]:
        if self.vector_index is not None:
            raw = self.vector_index.score(query, records)
            return {record.id: _clamp_score(raw.get(record.id, 0.0)) for record in records}
        query_terms = _tokens(query)
        return {
            record.id: _lexical_score(query_terms, _tokens(record.retrieval_text))
            for record in records
        }

    @staticmethod
    def _rank_records(
        records: Sequence[MemoryRecord],
        sources: Mapping[str, MemorySource],
        relevance: Mapping[str, float],
    ) -> list[tuple[MemoryRecord, float, float, float, float]]:
        ordered = sorted(records, key=lambda record: (_utc_iso(record.created_at), record.id))
        denominator = max(1, len(ordered) - 1)
        recency = {record.id: index / denominator for index, record in enumerate(ordered)}
        ranked: list[tuple[MemoryRecord, float, float, float, float]] = []
        for record in records:
            relevance_score = relevance.get(record.id, 0.0)
            source_quality = _source_quality(
                [sources[source_id] for source_id in record.source_ids]
            )
            score = _clamp_score(
                0.50 * relevance_score
                + 0.15 * record.importance
                + 0.10 * record.confidence
                + 0.10 * recency[record.id]
                + 0.15 * source_quality
            )
            ranked.append((record, score, relevance_score, recency[record.id], source_quality))
        return sorted(ranked, key=lambda item: (-item[1], item[0].id))

    @staticmethod
    def _retrieval_reason(record: MemoryRecord, relevance: float, source_quality: float) -> str:
        return (
            f"kind={record.kind.value}; status={record.status.value}; "
            f"lexical_or_vector_relevance={relevance:.3f}; "
            f"importance={record.importance:.3f}; confidence={record.confidence:.3f}; "
            f"source_quality={source_quality:.3f}"
        )


def _model_json(model: MemorySource | MemoryRecord | CompressionReport) -> str:
    return model.model_dump_json()


def _source_fingerprint(source: MemorySource) -> str:
    payload = source.model_dump(mode="json", exclude={"id", "captured_at", "schema_version"})
    return _hash_payload(payload)


def _record_identity(record: MemoryRecord) -> str:
    return _hash_payload(
        {
            "task_id": record.task_id,
            "kind": record.kind.value,
            "scope": record.scope.value,
            "scope_id": record.scope_id,
            "content_hash": record.content_hash,
            "source_ids": sorted(record.source_ids),
        }
    )


def _compaction_fingerprint(report: CompressionReport) -> str:
    payload = report.model_dump(mode="json", exclude={"id", "created_at", "schema_version"})
    return _hash_payload(payload)


def _hash_payload(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _append_in_filter(
    clauses: list[str], parameters: list[object], column: str, values: Sequence[object]
) -> None:
    placeholders = ",".join("?" for _ in values)
    clauses.append(f"{column} IN ({placeholders})")
    parameters.extend(values)


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in _TOKEN_PATTERN.findall(value)}


def _lexical_score(query: set[str], document: set[str]) -> float:
    if not query:
        return 0.0
    return len(query & document) / len(query)


def _source_quality(sources: Sequence[MemorySource]) -> float:
    if not sources:
        return 0.5
    weights = {
        MemorySourceKind.EVENT: 0.9,
        MemorySourceKind.TOOL_RESULT: 1.0,
        MemorySourceKind.USER_MESSAGE: 1.0,
        MemorySourceKind.CHECKPOINT: 0.95,
        MemorySourceKind.MEMORY_RECORD: 0.7,
    }
    return sum(weights[source.kind] for source in sources) / len(sources)


def _clamp_score(value: float) -> float:
    return round(min(1.0, max(0.0, value)), 6)
