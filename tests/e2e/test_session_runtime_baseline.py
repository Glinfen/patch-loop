import json
import multiprocessing
from pathlib import Path

import pytest

from patchloop.domain import Task, ToolResult
from patchloop.events import EventLogger
from patchloop.memory.manager import MemoryManagerSnapshot
from patchloop.persistence import RuntimeCheckpoint, SQLiteStore
from patchloop.prompt_cache import CacheEpochSnapshot
from tests.support.session_faults import FaultPoint, run_legacy_fault_worker

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "session_legacy"


def test_session_legacy_fixture_is_readable_by_current_models() -> None:
    completed = Task.model_validate_json(
        (FIXTURE_ROOT / "completed_task.json").read_text(encoding="utf-8")
    )
    running = Task.model_validate_json(
        (FIXTURE_ROOT / "running_task.json").read_text(encoding="utf-8")
    )
    checkpoint = RuntimeCheckpoint.model_validate_json(
        (FIXTURE_ROOT / "checkpoint.json").read_text(encoding="utf-8")
    )
    memory = MemoryManagerSnapshot.model_validate_json(
        (FIXTURE_ROOT / "memory_snapshot.json").read_text(encoding="utf-8")
    )
    cache = CacheEpochSnapshot.model_validate_json(
        (FIXTURE_ROOT / "cache_snapshot.json").read_text(encoding="utf-8")
    )
    trace_path = FIXTURE_ROOT / "trace.jsonl"

    assert completed.status.value == "completed"
    assert running.status.value == "running"
    assert running.plan is not None
    assert any(item.status.value != "completed" for item in running.plan.items)
    assert checkpoint.tool_history[0] == ToolResult.model_validate_json(
        (FIXTURE_ROOT / "confirmed_write_result.json").read_text(encoding="utf-8")
    )
    assert checkpoint.memory_manager == memory
    assert checkpoint.cache_epoch_state == cache
    assert [event.type for event in EventLogger(trace_path).read()] == [
        "task.started",
        "tool.completed",
        "task.completed",
        "task.started",
    ]


@pytest.mark.parametrize("attempt", range(3))
def test_legacy_fault_barrier_proves_external_action_before_result_commit(
    tmp_path: Path, attempt: int
) -> None:
    root = tmp_path / f"attempt-{attempt}"
    root.mkdir()
    database = root / "patchloop.db"
    target = root / "workspace" / "state.txt"
    audit = root / "audit.jsonl"
    context = multiprocessing.get_context("spawn")
    worker = context.Process(
        target=run_legacy_fault_worker,
        args=(str(database), str(target), str(audit), FaultPoint.AFTER_EXTERNAL_ACTION.value),
    )

    worker.start()
    worker.join(timeout=30)

    assert not worker.is_alive()
    assert worker.exitcode == 97
    audit_records = [
        json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert any(record["kind"] == "external_action_completed" for record in audit_records)
    assert any(
        record["kind"] == "barrier" and record["point"] == FaultPoint.AFTER_EXTERNAL_ACTION.value
        for record in audit_records
    )
    store = SQLiteStore(database)
    task_id = next(
        record["task_id"]
        for record in audit_records
        if record["kind"] == "barrier" and record["point"] == FaultPoint.INTENT_SUBMITTED.value
    )
    assert store.get_tool_result(task_id, "legacy-write-call") is None
    assert target.read_text(encoding="utf-8") == "external action completed\n"
