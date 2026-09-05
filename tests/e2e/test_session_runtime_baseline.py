import hashlib
import json
import multiprocessing
import shutil
import sqlite3
from pathlib import Path

import pytest

from patchloop.domain import Task, ToolResult
from patchloop.events import EventLogger
from patchloop.memory.manager import MemoryManagerSnapshot
from patchloop.persistence import RuntimeCheckpoint, SQLiteStore
from patchloop.prompt_cache import CacheEpochSnapshot
from tests.support.session_faults import FaultPoint, run_runtime_fault_worker

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


def test_session_legacy_database_is_readable_by_current_store(tmp_path: Path) -> None:
    source = FIXTURE_ROOT / "runtime-v0.sqlite"
    manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(source.read_bytes()).hexdigest() == manifest["database"]["sha256"]
    database = tmp_path / source.name
    shutil.copyfile(source, database)

    store = SQLiteStore(database)

    assert store.get_task("legacy-completed-task").status.value == "completed"
    assert store.get_task("legacy-running-task").status.value == "running"
    assert store.get_tool_result("legacy-running-task", "legacy-write-call") is not None
    assert store.get_checkpoint("legacy-running-task").next_step_index == 1
    assert [step.index for step in store.list_steps("legacy-running-task")] == [0]
    assert [path.as_posix() for path in store.list_artifacts("legacy-running-task")] == [
        "tests/fixtures/session_legacy/workspace/calculator.py"
    ]
    with sqlite3.connect(database) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        row_counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in manifest["row_counts"]
        }
    assert {
        "tasks",
        "agent_steps",
        "tool_calls",
        "checkpoints",
        "artifacts",
        "memory_sources",
        "memory_records",
        "memory_record_sources",
        "memory_compactions",
        "patchloop_schema_migrations",
    }.issubset(tables)
    assert row_counts == manifest["row_counts"]


def _run_fault_case(root: Path, point: FaultPoint) -> tuple[list[dict[str, object]], SQLiteStore]:
    root.mkdir()
    database = root / "patchloop.db"
    target = root / "workspace" / "state.txt"
    audit = root / "audit.jsonl"
    context = multiprocessing.get_context("spawn")
    worker = context.Process(
        target=run_runtime_fault_worker,
        args=(str(database), str(target), str(audit), point.value),
    )

    worker.start()
    worker.join(timeout=30)

    assert not worker.is_alive()
    assert worker.exitcode == 97
    audit_records = [
        json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert any(
        record["kind"] == "barrier" and record["point"] == point.value for record in audit_records
    )
    return audit_records, SQLiteStore(database)


@pytest.mark.parametrize(
    ("point", "action_completed", "result_committed"),
    [
        (FaultPoint.TOOL_DISPATCHED, False, False),
        (FaultPoint.BEFORE_EXTERNAL_ACTION, False, False),
        (FaultPoint.AFTER_EXTERNAL_ACTION, True, False),
        (FaultPoint.RESULT_SUBMITTED, True, True),
        (FaultPoint.BEFORE_CHECKPOINT_COMMIT, True, True),
    ],
)
def test_runtime_fault_matrix_hits_real_execution_boundaries(
    tmp_path: Path,
    point: FaultPoint,
    action_completed: bool,
    result_committed: bool,
) -> None:
    root = tmp_path / point.value
    audit_records, store = _run_fault_case(root, point)
    target = root / "workspace" / "state.txt"

    assert (
        any(record["kind"] == "external_action_completed" for record in audit_records)
        is action_completed
    )
    assert target.exists() is action_completed
    if action_completed:
        assert target.read_text(encoding="utf-8").splitlines() == ["legacy-write-call"]
    assert (
        store.get_tool_result("legacy-running-task", "legacy-write-call") is not None
    ) is result_committed
    assert store.get_checkpoint("legacy-running-task").next_step_index == 0


@pytest.mark.parametrize("attempt", range(3))
def test_runtime_fault_barrier_repeats_external_action_gap(tmp_path: Path, attempt: int) -> None:
    root = tmp_path / f"attempt-{attempt}"
    audit_records, store = _run_fault_case(root, FaultPoint.AFTER_EXTERNAL_ACTION)
    target = root / "workspace" / "state.txt"

    assert sum(record["kind"] == "external_action_completed" for record in audit_records) == 1
    assert target.read_text(encoding="utf-8").splitlines() == ["legacy-write-call"]
    assert store.get_tool_result("legacy-running-task", "legacy-write-call") is None
