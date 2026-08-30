import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from patchloop.domain import ErrorKind, Plan, PlanItem, StepStatus, Task, ToolCall, ToolResult
from patchloop.memory import (
    CompressionOperation,
    CompressionReport,
    EpisodicMemoryManager,
    MemoryKind,
    MemoryQuery,
    MemoryRecord,
    MemorySource,
    MemorySourceKind,
    MemoryStatus,
    MemoryStoreError,
    SQLiteMemoryStore,
    validate_supersession_chain,
)
from patchloop.persistence import SQLiteStore
from patchloop.storage import TaskNotFoundError

NOW = datetime(2026, 8, 30, tzinfo=UTC)


class FixedVectorIndex:
    def __init__(self, scores: Mapping[str, float]) -> None:
        self.scores = scores
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, records: Sequence[MemoryRecord]) -> Mapping[str, float]:
        self.calls.append((query, [record.id for record in records]))
        return self.scores


def create_store(tmp_path: Path) -> tuple[SQLiteStore, Task]:
    store = SQLiteStore(tmp_path / "patchloop.db")
    task = Task(id="task-1", goal="Persist memory", repository=str(tmp_path))
    store.save_task(task)
    return store, task


def memory_source(
    task_id: str,
    source_id: str = "source-1",
    *,
    step: int = 3,
    path: str = "src/service.py",
) -> MemorySource:
    return MemorySource(
        id=source_id,
        task_id=task_id,
        kind=MemorySourceKind.TOOL_RESULT,
        evidence_hash="a" * 64,
        event_id="event-1",
        tool_call_id="call-1",
        step_index=step,
        path=path,
        line_start=10,
        captured_at=NOW,
    )


def memory_record(
    task_id: str,
    source_id: str = "source-1",
    record_id: str = "record-1",
    *,
    text: str = "Current payment API contract",
    created_at: datetime = NOW,
) -> MemoryRecord:
    return MemoryRecord(
        id=record_id,
        task_id=task_id,
        kind=MemoryKind.SEMANTIC,
        scope_id=task_id,
        content={"fact": text},
        retrieval_text=text,
        source_ids=[source_id],
        importance=0.9,
        confidence=0.85,
        estimated_tokens=20,
        created_at=created_at,
    )


def table_count(path: Path, table: str) -> int:
    with closing(sqlite3.connect(path)) as connection, connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def test_memory_store_requires_existing_runtime_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"

    with pytest.raises(MemoryStoreError, match="existing PatchLoop database"):
        SQLiteMemoryStore(missing)

    assert not missing.exists()


def test_old_runtime_database_migrates_without_rewriting_tasks(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    task = Task(id="legacy-task", goal="Keep me", repository=str(tmp_path))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?)",
            (task.id, task.status, task.model_dump_json(), task.updated_at.isoformat()),
        )

    store = SQLiteStore(path)

    assert store.get_task(task.id).goal == "Keep me"
    with closing(sqlite3.connect(path)) as connection, connection:
        version = connection.execute(
            "SELECT version FROM patchloop_schema_migrations WHERE component = 'memory'"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'memory_%'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_memory_%'"
            )
        }
    assert version == (1,)
    assert tables == {
        "memory_sources",
        "memory_records",
        "memory_record_sources",
        "memory_compactions",
    }
    assert {
        "idx_memory_sources_task_step",
        "idx_memory_sources_task_path",
        "idx_memory_records_task_kind_status",
        "idx_memory_records_task_content_hash",
        "idx_memory_records_task_scope",
        "idx_memory_compactions_task_generation",
    }.issubset(indexes)


def test_memory_store_round_trips_and_queries_indexed_fields(tmp_path: Path) -> None:
    store, task = create_store(tmp_path)
    evidence = memory_source(task.id)
    record = memory_record(task.id)

    result = store.memory.save_batch(sources=[evidence], records=[record])
    bundle = store.memory.query(
        MemoryQuery(
            task_id=task.id,
            text="payment API contract",
            kinds=[MemoryKind.SEMANTIC],
            statuses=[MemoryStatus.ACTIVE],
            paths=["src/service.py"],
            step_start=2,
            step_end=4,
            token_budget=100,
        )
    )

    assert result.sources == (evidence,)
    assert result.records == (record,)
    assert store.memory.get_source(evidence.id) == evidence
    assert store.memory.get_record(record.id) == record
    assert [hit.record.id for hit in bundle.hits] == [record.id]
    assert store.memory.find_records_by_content_hash(task.id, record.content_hash) == [record]
    assert not store.memory.query(
        MemoryQuery(
            task_id=task.id,
            text="contract",
            kinds=[MemoryKind.SEMANTIC],
            step_start=20,
        )
    ).hits


def test_repeated_checkpoint_restore_is_idempotent_across_new_ids(tmp_path: Path) -> None:
    store, task = create_store(tmp_path)
    first_source = memory_source(task.id)
    first_record = memory_record(task.id)
    first = store.memory.restore_checkpoint_state(sources=[first_source], records=[first_record])
    replay_source = memory_source(task.id, "source-from-replay")
    replay_record = memory_record(
        task.id,
        replay_source.id,
        "record-from-replay",
        created_at=NOW + timedelta(minutes=5),
    )

    replay = store.memory.restore_checkpoint_state(sources=[replay_source], records=[replay_record])

    assert replay.sources[0].id == first.sources[0].id
    assert replay.records[0].id == first.records[0].id
    assert len(store.memory.list_sources(task.id)) == 1
    assert len(store.memory.list_records(task.id)) == 1


def test_failed_batch_rolls_back_sources_written_before_invalid_record(tmp_path: Path) -> None:
    store, task = create_store(tmp_path)
    evidence = memory_source(task.id, "source-rollback")
    invalid_record = memory_record(task.id, "source-does-not-exist", "record-invalid")

    with pytest.raises(MemoryStoreError, match="unknown sources"):
        store.memory.save_batch(sources=[evidence], records=[invalid_record])

    assert store.memory.list_sources(task.id) == []
    assert store.memory.list_records(task.id) == []


def test_memory_payload_is_redacted_before_hashing_and_persistence(tmp_path: Path) -> None:
    store, task = create_store(tmp_path)
    secret = "sk-" + "X" * 24
    evidence = memory_source(task.id, path=f"logs/{secret}.txt")
    record = memory_record(task.id, text=f"credential={secret}")

    result = store.memory.save_batch(sources=[evidence], records=[record])
    database_bytes = store.path.read_bytes()

    assert secret not in result.sources[0].path  # type: ignore[operator]
    assert secret not in result.records[0].retrieval_text
    assert result.records[0].content_hash != record.content_hash
    assert secret.encode() not in database_bytes


def test_compaction_reports_reference_persisted_records_and_round_trip(tmp_path: Path) -> None:
    store, task = create_store(tmp_path)
    evidence = memory_source(task.id)
    first = memory_record(task.id, record_id="record-1", text="first fact")
    second = memory_record(task.id, record_id="record-2", text="duplicate fact")
    compressed = MemoryRecord(
        id="record-3",
        task_id=task.id,
        kind=MemoryKind.SEMANTIC,
        scope_id=task.id,
        content={"summary": "combined facts"},
        retrieval_text="combined facts",
        source_ids=[evidence.id],
        compression_generation=1,
        derived_from_ids=[first.id, second.id],
        estimated_tokens=10,
        created_at=NOW + timedelta(minutes=1),
    )
    report = CompressionReport(
        id="compression-1",
        task_id=task.id,
        generation=1,
        operations=[CompressionOperation.DEDUPLICATE, CompressionOperation.SUMMARIZE],
        input_record_ids=[first.id, second.id],
        output_record_ids=[first.id, compressed.id],
        protected_record_ids=[first.id],
        removed_record_ids=[second.id],
        input_tokens=40,
        output_tokens=30,
        created_at=NOW + timedelta(minutes=1),
    )

    result = store.memory.save_batch(
        sources=[evidence], records=[first, second, compressed], compactions=[report]
    )

    assert result.compactions == (report,)
    assert store.memory.list_compactions(task.id) == [report]


def test_record_lifecycle_update_and_replacement_chain_are_persisted(tmp_path: Path) -> None:
    store, task = create_store(tmp_path)
    evidence = memory_source(task.id)
    old = memory_record(task.id, record_id="record-old", text="old contract")
    store.memory.save_batch(sources=[evidence], records=[old])
    old_payload = old.model_dump()
    old_payload.update(status=MemoryStatus.SUPERSEDED, superseded_by_id="record-new")
    superseded = MemoryRecord.model_validate(old_payload)
    replacement_payload = memory_record(
        task.id,
        record_id="record-new",
        text="new contract",
        created_at=NOW + timedelta(seconds=1),
    ).model_dump()
    replacement_payload["supersedes_id"] = old.id
    replacement = MemoryRecord.model_validate(replacement_payload)

    store.memory.save_batch(records=[superseded, replacement])

    records = store.memory.list_records(task.id)
    validate_supersession_chain(records)
    assert store.memory.get_record(old.id).status is MemoryStatus.SUPERSEDED
    active = store.memory.query(
        MemoryQuery(
            task_id=task.id,
            text="contract",
            kinds=[MemoryKind.SEMANTIC],
            token_budget=100,
        )
    )
    assert [hit.record.id for hit in active.hits] == [replacement.id]


def test_optional_vector_index_controls_relevance_without_network_dependency(
    tmp_path: Path,
) -> None:
    runtime, task = create_store(tmp_path)
    first_source = memory_source(task.id, "source-1", step=1)
    second_payload = memory_source(task.id, "source-2", step=2).model_dump()
    second_payload.update(tool_call_id="call-2", event_id="event-2", evidence_hash="b" * 64)
    second_source = MemorySource.model_validate(second_payload)
    first = memory_record(task.id, first_source.id, "record-1", text="first unrelated")
    second = memory_record(task.id, second_source.id, "record-2", text="second unrelated")
    vector = FixedVectorIndex({first.id: 0.1, second.id: 1.0})
    store = SQLiteMemoryStore(runtime.path, vector_index=vector)
    store.save_batch(sources=[first_source, second_source], records=[first, second])

    bundle = store.query(
        MemoryQuery(
            task_id=task.id,
            text="semantic target",
            kinds=[MemoryKind.SEMANTIC],
            token_budget=100,
        )
    )

    assert [hit.record.id for hit in bundle.hits] == [second.id, first.id]
    assert vector.calls == [("semantic target", [first.id, second.id])]


def test_deleting_task_cascades_all_memory_rows(tmp_path: Path) -> None:
    store, task = create_store(tmp_path)
    evidence = memory_source(task.id)
    record = memory_record(task.id)
    store.memory.save_batch(sources=[evidence], records=[record])
    report = CompressionReport(
        task_id=task.id,
        generation=1,
        operations=[CompressionOperation.DEDUPLICATE],
        input_record_ids=[record.id],
        output_record_ids=[record.id],
        protected_record_ids=[record.id],
        input_tokens=20,
        output_tokens=20,
    )
    store.memory.save_compaction(report)

    store.delete_task(task.id)

    with pytest.raises(TaskNotFoundError):
        store.get_task(task.id)
    for table in (
        "memory_sources",
        "memory_records",
        "memory_record_sources",
        "memory_compactions",
    ):
        assert table_count(store.path, table) == 0


def test_episode_query_filters_error_phase_path_outcome_and_time(tmp_path: Path) -> None:
    store, task = create_store(tmp_path)
    episodes = EpisodicMemoryManager(task.id, task.goal)
    plan = Plan(
        items=[
            PlanItem(
                id="repair",
                description="Repair payment parser",
                status=StepStatus.RUNNING,
            )
        ]
    )
    call = ToolCall(
        id="failed-parser-write",
        name="apply_patch",
        arguments={"path": "src/parser.py"},
    )
    write = episodes.observe_tool(
        call,
        ToolResult(
            call_id=call.id,
            tool_name=call.name,
            success=False,
            output="target text not found",
            error_kind=ErrorKind.EXECUTION_ERROR,
        ),
        step_index=7,
        plan=plan,
        changed_paths=[],
    )
    assert write is not None
    store.memory.save_batch(sources=write.sources, records=[write.record])

    bundle = store.memory.query(
        MemoryQuery(
            task_id=task.id,
            text="parser target failure",
            kinds=[MemoryKind.EPISODIC],
            paths=["src/parser.py"],
            step_start=7,
            step_end=7,
            created_after=datetime.now(UTC) - timedelta(minutes=1),
            error_kinds=[ErrorKind.EXECUTION_ERROR],
            plan_phases=["Repair payment parser"],
            episode_outcomes=["failed"],
        )
    )

    assert [hit.record.id for hit in bundle.hits] == [write.record.id]
    assert bundle.sources[0].path == "src/parser.py"
    assert (
        store.memory.query(
            MemoryQuery(
                task_id=task.id,
                text="parser",
                error_kinds=[ErrorKind.TEST_FAILURE],
            )
        ).hits
        == []
    )


def test_newer_memory_migration_fails_without_destroying_runtime_data(tmp_path: Path) -> None:
    store, task = create_store(tmp_path)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(
            "UPDATE patchloop_schema_migrations SET version = 99 WHERE component = 'memory'"
        )

    with pytest.raises(MemoryStoreError, match="newer than supported"):
        SQLiteMemoryStore(store.path)

    with closing(sqlite3.connect(store.path)) as connection, connection:
        row = connection.execute(
            "SELECT payload_json FROM tasks WHERE id = ?", (task.id,)
        ).fetchone()
    assert row is not None
    assert Task.model_validate_json(row[0]).goal == task.goal


def test_partial_memory_migration_failure_rolls_back_schema_not_tasks(tmp_path: Path) -> None:
    path = tmp_path / "broken-legacy.db"
    task = Task(id="legacy-task", goal="Survive migration", repository=str(tmp_path))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?)",
            (task.id, task.status, task.model_dump_json(), task.updated_at.isoformat()),
        )
        connection.execute("CREATE TABLE memory_sources (broken TEXT)")

    with pytest.raises(MemoryStoreError, match="migration failed"):
        SQLiteStore(path)

    with closing(sqlite3.connect(path)) as connection, connection:
        task_row = connection.execute(
            "SELECT payload_json FROM tasks WHERE id = ?", (task.id,)
        ).fetchone()
        migration_table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'patchloop_schema_migrations'
            """
        ).fetchone()
    assert task_row is not None
    assert Task.model_validate_json(task_row[0]).goal == task.goal
    assert migration_table is None
