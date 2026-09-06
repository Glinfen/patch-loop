"""SRF-02: centralized SQLite connection entry and runtime schema migration."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import patchloop.persistence
from patchloop.memory.store import MemoryStoreError, initialize_memory_schema
from patchloop.persistence import (
    RUNTIME_SCHEMA_VERSION,
    RuntimeSchemaError,
    SQLiteStore,
    initialize_runtime_schema,
)
from patchloop.sqlite_support import (
    BUSY_TIMEOUT_SECONDS,
    SQLiteConfigurationError,
    connect,
    connect_write,
    open_connection,
    verify_connection_config,
)

_RUNTIME_DATA_TABLES = {"tasks", "agent_steps", "tool_calls", "checkpoints", "artifacts"}


def _table_names(database: Path) -> set[str]:
    with closing(sqlite3.connect(database)) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def _migration_versions(database: Path) -> dict[str, int]:
    with closing(sqlite3.connect(database)) as connection:
        return {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT component, version FROM patchloop_schema_migrations"
            ).fetchall()
        }


def test_open_connection_reads_back_required_configuration(tmp_path: Path) -> None:
    database = tmp_path / "configured.db"

    with closing(open_connection(database)) as connection:
        assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        assert int(connection.execute("PRAGMA busy_timeout").fetchone()[0]) == round(
            BUSY_TIMEOUT_SECONDS * 1000
        )
        assert connection.row_factory is sqlite3.Row


def test_verify_connection_config_rejects_unconfigured_connection(tmp_path: Path) -> None:
    database = tmp_path / "plain.db"

    with (
        closing(sqlite3.connect(database)) as connection,
        pytest.raises(SQLiteConfigurationError, match="journal_mode=delete"),
    ):
        verify_connection_config(connection)


def test_connect_commits_on_success_and_rolls_back_on_failure(tmp_path: Path) -> None:
    database = tmp_path / "transactional.db"
    with connect(database) as connection:
        connection.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT)")

    with pytest.raises(RuntimeError, match="injected"), connect(database) as connection:
        connection.execute("INSERT INTO probe (value) VALUES ('rolled-back')")
        raise RuntimeError("injected")

    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0


def test_connect_write_commits_and_rolls_back_immediate_transactions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "write.db"
    with connect_write(database) as connection:
        connection.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO probe (value) VALUES ('kept')")

    with pytest.raises(RuntimeError, match="injected"), connect_write(database) as connection:
        connection.execute("INSERT INTO probe (value) VALUES ('rolled-back')")
        raise RuntimeError("injected")

    with connect(database) as connection:
        rows = connection.execute("SELECT value FROM probe ORDER BY id").fetchall()
    assert [row[0] for row in rows] == ["kept"]


def test_runtime_schema_migration_runs_before_memory_migration(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"

    SQLiteStore(database)

    assert _migration_versions(database) == {"runtime": RUNTIME_SCHEMA_VERSION, "memory": 1}
    assert _RUNTIME_DATA_TABLES.issubset(_table_names(database))


def test_memory_migration_still_requires_runtime_tables(tmp_path: Path) -> None:
    database = tmp_path / "empty.db"

    with (
        closing(open_connection(database)) as connection,
        pytest.raises(MemoryStoreError, match="initialized PatchLoop tasks table"),
    ):
        initialize_memory_schema(connection)


def test_future_runtime_schema_version_refuses_to_start(tmp_path: Path) -> None:
    database = tmp_path / "future.db"
    SQLiteStore(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE patchloop_schema_migrations SET version = ? WHERE component = 'runtime'",
            (RUNTIME_SCHEMA_VERSION + 1,),
        )

    with pytest.raises(RuntimeSchemaError, match="newer than supported"):
        SQLiteStore(database)

    assert "tasks" in _table_names(database)


def test_incomplete_runtime_schema_refuses_to_start(tmp_path: Path) -> None:
    database = tmp_path / "incomplete.db"
    SQLiteStore(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TABLE artifacts")

    with pytest.raises(RuntimeSchemaError, match="incomplete"):
        SQLiteStore(database)


def test_incomplete_task_projection_columns_refuse_to_start(tmp_path: Path) -> None:
    database = tmp_path / "missing-columns.db"
    SQLiteStore(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("ALTER TABLE tasks DROP COLUMN outcome")

    with pytest.raises(RuntimeSchemaError, match="missing columns"):
        SQLiteStore(database)


def _migration_with_trailing_failure() -> tuple[str, ...]:
    """Run the complete real migration, then fail on the final statement."""

    return (*patchloop.persistence._RUNTIME_MIGRATION_V1, "CREATE TABLE broken (")


def test_runtime_migration_failure_rolls_back_fresh_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "failing.db"
    monkeypatch.setattr(
        patchloop.persistence, "_RUNTIME_MIGRATION_V1", _migration_with_trailing_failure()
    )

    with pytest.raises(RuntimeSchemaError, match="runtime schema migration failed"):
        SQLiteStore(database)

    tables = _table_names(database)
    assert "tasks" not in tables
    assert "sessions" not in tables
    assert "patchloop_schema_migrations" not in tables


def test_runtime_migration_failure_preserves_legacy_task_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "legacy.db"
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
            ("legacy-task", "created", "{}", "2026-01-01T00:00:00+00:00"),
        )
    monkeypatch.setattr(
        patchloop.persistence, "_RUNTIME_MIGRATION_V1", _migration_with_trailing_failure()
    )

    with pytest.raises(RuntimeSchemaError, match="runtime schema migration failed"):
        SQLiteStore(database)

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert connection.execute("SELECT payload_json FROM tasks").fetchone()[0] == "{}"
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'patchloop_schema_migrations'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
            ).fetchone()
            is None
        )


def test_initialize_runtime_schema_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "repeat.db"

    with closing(open_connection(database)) as connection:
        initialize_runtime_schema(connection)
        initialize_runtime_schema(connection)
        rows = connection.execute("SELECT COUNT(*) FROM patchloop_schema_migrations").fetchone()[0]

    assert rows == 1
    assert _migration_versions(database) == {"runtime": RUNTIME_SCHEMA_VERSION}
