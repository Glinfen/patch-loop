from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from patchloop.domain import Task, ToolCall, ToolResult
from patchloop.memory import (
    MemoryCompressionPolicy,
    MemoryEventCursor,
    MemoryManager,
    MemoryRecord,
    MemorySource,
    MemoryStoreError,
)
from patchloop.persistence import SQLiteStore


class CompressionFailingStore:
    def __init__(self) -> None:
        self.records: dict[str, MemoryRecord] = {}
        self.sources: dict[str, MemorySource] = {}

    def list_records(self, task_id: str) -> list[MemoryRecord]:
        return [record for record in self.records.values() if record.task_id == task_id]

    def list_sources(self, task_id: str) -> list[MemorySource]:
        return [source for source in self.sources.values() if source.task_id == task_id]

    def save_batch(
        self,
        *,
        sources: Sequence[MemorySource] = (),
        records: Sequence[MemoryRecord] = (),
        compactions: Sequence[object] = (),
    ) -> object:
        if compactions:
            raise MemoryStoreError("simulated compaction transaction failure")
        self.sources.update((source.id, source) for source in sources)
        self.records.update((record.id, record) for record in records)
        return None


def test_event_cursor_rejects_ambiguous_recovery_positions() -> None:
    with pytest.raises(ValidationError, match="index must equal processed event count"):
        MemoryEventCursor(
            next_event_index=2,
            last_event_id="tool:one",
            processed_event_ids=["tool:one"],
        )

    with pytest.raises(ValidationError, match="both processed and pending"):
        MemoryEventCursor(
            next_event_index=1,
            last_event_id="tool:one",
            processed_event_ids=["tool:one"],
            pending_event_ids=["tool:one"],
        )


def test_manager_snapshot_replays_tool_event_without_duplicate_memory(tmp_path: Path) -> None:
    task = Task(id="manager-replay", goal="Inspect config", repository=str(tmp_path))
    runtime = SQLiteStore(tmp_path / "state.db")
    runtime.save_task(task)
    manager = MemoryManager(
        task.id,
        task.goal,
        task.repository,
        working_token_budget=512,
        store=runtime.memory,
    )
    initial = manager.ingest_initial()
    call = ToolCall(id="read-once", name="read_file", arguments={"path": "config.py"})
    result = ToolResult(
        call_id=call.id,
        tool_name=call.name,
        success=True,
        output="1: MODE = 'safe'",
    )
    written = manager.ingest_tool(
        call,
        result,
        step_index=0,
        plan=None,
        changed_paths=[],
        diff="",
    )
    snapshot = manager.snapshot()
    record_ids = {record.id for record in runtime.memory.list_records(task.id)}

    restored = MemoryManager(
        task.id,
        task.goal,
        task.repository,
        working_token_budget=512,
        store=runtime.memory,
        snapshot=snapshot,
    )
    replay = restored.ingest_tool(
        call,
        result,
        step_index=0,
        plan=None,
        changed_paths=[],
        diff="",
    )

    assert initial.event_index == 0
    assert written.event_index == 1
    assert replay.duplicate
    assert replay.written_record_ids == ()
    assert restored.snapshot().cursor == snapshot.cursor
    assert {record.id for record in runtime.memory.list_records(task.id)} == record_ids


def test_manager_triggers_bounded_compression_at_active_watermark() -> None:
    manager = MemoryManager(
        "manager-compress",
        "Inspect repeated output",
        "repository",
        working_token_budget=512,
        compression_policy=MemoryCompressionPolicy(
            active_uncompressed_threshold=4,
            active_generation_threshold=100,
        ),
    )
    manager.ingest_initial()
    updates = []
    for index in range(4):
        call = ToolCall(
            id=f"read-{index}",
            name="read_file",
            arguments={"path": "src/service.py"},
        )
        updates.append(
            manager.ingest_tool(
                call,
                ToolResult(
                    call_id=call.id,
                    tool_name=call.name,
                    success=True,
                    output="repeated service observation",
                ),
                step_index=index,
                plan=None,
                changed_paths=[],
                diff="",
            )
        )

    compaction = updates[-1].compaction
    snapshot = manager.snapshot()
    assert compaction is not None
    assert compaction.report is not None
    assert compaction.report.generation == 1
    assert snapshot.compactions == 1
    assert snapshot.compression_output_tokens < snapshot.compression_input_tokens
    assert snapshot.records_written == 5
    assert sum(snapshot.records_by_kind.values()) == snapshot.records_written
    assert snapshot.records_by_status["active"] > 0
    assert snapshot.compression_duration_ms > 0
    assert snapshot.cursor.next_event_index == 5
    assert snapshot.cursor.pending_event_ids == []


def test_tiny_retrieval_budget_uses_legacy_context_without_disabling_memory() -> None:
    manager = MemoryManager(
        "manager-small-context",
        "Inspect config",
        "repository",
        working_token_budget=128,
    )
    manager.ingest_initial()
    call = ToolCall(id="small-read", name="read_file", arguments={"path": "config.py"})
    manager.ingest_tool(
        call,
        ToolResult(
            call_id=call.id,
            tool_name=call.name,
            success=True,
            output="1: MODE = 'safe'",
        ),
        step_index=0,
        plan=None,
        changed_paths=[],
        diff="",
    )

    retrieval = manager.retrieve(
        plan=None,
        changed_paths=[],
        total_context_tokens=256,
        retrieval_token_cap=1,
    )

    assert retrieval.context is None
    assert retrieval.fallback_reason is None
    assert not manager.fallback_active
    assert manager.snapshot().fallback_count == 0


def test_failed_compaction_preserves_active_records_and_activates_fallback() -> None:
    store = CompressionFailingStore()
    manager = MemoryManager(
        "manager-compression-failure",
        "Inspect repeated output",
        "repository",
        working_token_budget=512,
        store=store,
        compression_policy=MemoryCompressionPolicy(
            active_uncompressed_threshold=4,
            active_generation_threshold=100,
        ),
    )
    manager.ingest_initial()
    final_update = None
    for index in range(4):
        call = ToolCall(
            id=f"failed-compression-read-{index}",
            name="read_file",
            arguments={"path": "src/service.py"},
        )
        final_update = manager.ingest_tool(
            call,
            ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=True,
                output="repeated service observation",
            ),
            step_index=index,
            plan=None,
            changed_paths=[],
            diff="",
        )

    assert final_update is not None
    assert final_update.compaction is None
    assert final_update.fallback_reason is not None
    assert "compression failed" in final_update.fallback_reason
    assert manager.fallback_active
    assert all(record.status.value == "active" for record in store.records.values())
    assert manager.snapshot().cursor.pending_event_ids == []
