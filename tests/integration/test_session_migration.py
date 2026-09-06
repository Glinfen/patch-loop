"""SRF-02 integration: legacy runtime-v0 database upgrade behavior.

The frozen fixture under ``tests/fixtures/session_legacy`` is the immutable
input; every test copies ``runtime-v0.sqlite`` before opening it so the
upgrade path runs against a real legacy database instead of a rebuilt one.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import patchloop.persistence
from patchloop.domain import Task, TaskRuntimeCondition
from patchloop.execution.models import Effect, EffectStatus
from patchloop.persistence import RUNTIME_SCHEMA_VERSION, RuntimeSchemaError, SQLiteStore
from patchloop.session.models import Session

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "session_legacy"


def _copy_legacy_database(tmp_path: Path) -> Path:
    source = FIXTURE_ROOT / "runtime-v0.sqlite"
    database = tmp_path / source.name
    shutil.copyfile(source, database)
    return database


def _table_row_counts(database: Path, tables: list[str]) -> dict[str, int]:
    with closing(sqlite3.connect(database)) as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


def _migration_versions(database: Path) -> dict[str, int]:
    with closing(sqlite3.connect(database)) as connection:
        return {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT component, version FROM patchloop_schema_migrations"
            ).fetchall()
        }


def _task_payloads(database: Path) -> dict[str, str]:
    with closing(sqlite3.connect(database)) as connection:
        return {
            str(row[0]): str(row[1])
            for row in connection.execute("SELECT id, payload_json FROM tasks").fetchall()
        }


def test_legacy_runtime_v0_database_upgrades_in_place(tmp_path: Path) -> None:
    manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))
    tables = list(manifest["row_counts"])
    database = _copy_legacy_database(tmp_path)
    legacy_counts = _table_row_counts(database, tables)

    store = SQLiteStore(database)

    assert store.get_task("legacy-completed-task").outcome.value == "completed"
    assert store.get_task("legacy-running-task").outcome.value == "active"
    assert _table_row_counts(database, tables) == legacy_counts | {"patchloop_schema_migrations": 2}
    assert _migration_versions(database) == {
        "runtime": RUNTIME_SCHEMA_VERSION,
        "memory": 1,
    }


def test_repeated_upgrade_does_not_duplicate_rows(tmp_path: Path) -> None:
    database = _copy_legacy_database(tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        ]

    SQLiteStore(database)
    first_counts = _table_row_counts(database, tables)
    SQLiteStore(database)
    second_counts = _table_row_counts(database, tables)

    assert first_counts == second_counts
    assert _table_row_counts(database, ["sessions", "effects", "session_events"]) == {
        "sessions": 2,
        "effects": 1,
        "session_events": 2,
    }


def test_legacy_upgrade_adds_session_schema_and_task_projections(tmp_path: Path) -> None:
    database = _copy_legacy_database(tmp_path)

    store = SQLiteStore(database)

    with closing(sqlite3.connect(database)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        projections = {
            str(row[0]): (str(row[1]), str(row[2]), int(row[3]), row[4])
            for row in connection.execute(
                """
                SELECT id, outcome, runtime_condition, version, session_id FROM tasks
                """
            ).fetchall()
        }
        task_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(tasks)")}
    assert {
        "sessions",
        "turns",
        "executions",
        "effects",
        "approvals",
        "control_requests",
        "recovery_dispositions",
        "session_events",
        "workspace_leases",
    }.issubset(tables)
    assert {"session_id", "outcome", "runtime_condition", "version"}.issubset(task_columns)
    assert projections["legacy-completed-task"] == (
        "completed",
        "ended",
        1,
        "legacy-session-legacy-completed-task",
    )
    assert projections["legacy-running-task"] == (
        "active",
        "recovery_required",
        1,
        "legacy-session-legacy-running-task",
    )
    assert store.get_task("legacy-running-task").runtime_condition is (
        TaskRuntimeCondition.RECOVERY_REQUIRED
    )


def test_legacy_tasks_map_one_to_one_to_sessions_and_confirmed_effects(tmp_path: Path) -> None:
    database = _copy_legacy_database(tmp_path)
    store = SQLiteStore(database)

    sessions = store.list_sessions()
    assert [session.id for session in sessions] == [
        "legacy-session-legacy-completed-task",
        "legacy-session-legacy-running-task",
    ]
    completed = store.get_session("legacy-session-legacy-completed-task")
    running = store.get_session("legacy-session-legacy-running-task")
    assert isinstance(completed, Session)
    assert completed.active_task_id is None
    assert running.active_task_id == "legacy-running-task"
    assert completed.workspace_ref == running.workspace_ref

    effects = store.list_effects("legacy-running-task")
    assert len(effects) == 1
    effect = effects[0]
    assert isinstance(effect, Effect)
    assert effect.status is EffectStatus.SUCCEEDED
    assert effect.provider_call_id == "legacy-write-call"
    assert effect.step_id == "legacy-step-0"
    assert effect.batch_position == 0
    assert effect.result_ref == "tool-result:legacy-running-task:legacy-write-call"
    assert store.list_effects("legacy-completed-task") == []


def test_legacy_checkpoint_and_task_associations_survive_session_mapping(tmp_path: Path) -> None:
    database = _copy_legacy_database(tmp_path)
    store = SQLiteStore(database)
    checkpoint = store.get_checkpoint("legacy-running-task")

    assert checkpoint.session_id == "legacy-session-legacy-running-task"
    assert checkpoint.event_sequence == 1
    assert checkpoint.plan is not None
    assert checkpoint.plan.revision == 2
    assert checkpoint.cache_epoch_state is not None
    assert checkpoint.cache_epoch_state.epoch_id == "legacy-epoch"
    assert checkpoint.memory_manager is not None
    assert checkpoint.memory_manager.cursor.last_event_id == "tool:legacy-write-call"
    assert store.list_artifacts("legacy-running-task") == [
        Path("tests\\fixtures\\session_legacy\\workspace\\calculator.py")
    ]
    assert [source.id for source in store.memory.list_sources("legacy-running-task")] == [
        "legacy-memory-source"
    ]
    assert [record.id for record in store.memory.list_records("legacy-running-task")] == [
        "legacy-memory-record"
    ]


def test_legacy_session_migration_journal_records_recovery_review(tmp_path: Path) -> None:
    database = _copy_legacy_database(tmp_path)
    store = SQLiteStore(database)

    completed_event = store.list_events("legacy-session-legacy-completed-task")
    running_event = store.list_events("legacy-session-legacy-running-task")
    assert len(completed_event) == len(running_event) == 1
    assert completed_event[0].type == running_event[0].type == "legacy.session.migrated"
    assert completed_event[0].data["requires_recovery_review"] is False
    assert running_event[0].data["requires_recovery_review"] is True
    assert running_event[0].data["effect_ids"] == [store.list_effects("legacy-running-task")[0].id]


def test_legacy_mapping_failure_rolls_back_all_runtime_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _copy_legacy_database(tmp_path)
    original_payloads = _task_payloads(database)

    def fail_mapping(_connection: sqlite3.Connection, _task: object) -> list[Effect]:
        raise RuntimeError("injected legacy mapping failure")

    monkeypatch.setattr(patchloop.persistence, "_legacy_effects", fail_mapping)
    with pytest.raises(RuntimeSchemaError, match="injected legacy mapping failure"):
        SQLiteStore(database)

    assert _task_payloads(database) == original_payloads
    with closing(sqlite3.connect(database)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        runtime_marker = connection.execute(
            "SELECT version FROM patchloop_schema_migrations WHERE component = 'runtime'"
        ).fetchone()
    assert "sessions" not in tables
    assert runtime_marker is None


def test_legacy_created_task_remains_idle_in_an_active_session(tmp_path: Path) -> None:
    database = tmp_path / "created-v0.sqlite"
    task = Task(id="legacy-created-task", goal="Not started", repository="workspace")
    with closing(sqlite3.connect(database)) as connection, connection:
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
            (task.id, task.status.value, task.model_dump_json(), task.updated_at.isoformat()),
        )

    store = SQLiteStore(database)
    migrated = store.get_task(task.id)

    assert migrated.runtime_condition is TaskRuntimeCondition.IDLE
    assert migrated.session_id == "legacy-session-legacy-created-task"
    assert store.get_session(migrated.session_id).active_task_id == task.id


def test_runtime_v1_database_receives_the_v2_legacy_session_mapping(tmp_path: Path) -> None:
    database = _copy_legacy_database(tmp_path)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in patchloop.persistence._RUNTIME_MIGRATION_V1:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO patchloop_schema_migrations (component, version, updated_at)
            VALUES ('runtime', 1, '2026-09-01T00:00:00+00:00')
            """
        )

    store = SQLiteStore(database)

    assert _migration_versions(database)["runtime"] == RUNTIME_SCHEMA_VERSION
    assert len(store.list_sessions()) == 2
    assert store.get_task("legacy-running-task").runtime_condition is (
        TaskRuntimeCondition.RECOVERY_REQUIRED
    )
