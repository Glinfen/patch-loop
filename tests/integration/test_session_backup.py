"""SRF-02 step 7 migration backup and explicit rollback workflow."""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

import patchloop.persistence
from patchloop.execution.models import Effect
from patchloop.persistence import RUNTIME_SCHEMA_VERSION, RuntimeSchemaError, SQLiteStore
from patchloop.session.models import Session
from patchloop.sqlite_support import restore_migration_backup, runtime_schema_version

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "session_legacy"


def _copy_legacy_database(tmp_path: Path) -> Path:
    database = tmp_path / "runtime-v0.sqlite"
    shutil.copyfile(FIXTURE_ROOT / "runtime-v0.sqlite", database)
    return database


def _tables(database: Path) -> set[str]:
    with closing(sqlite3.connect(database)) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def test_legacy_migration_creates_a_consistent_pre_upgrade_backup(tmp_path: Path) -> None:
    database = _copy_legacy_database(tmp_path)

    store = SQLiteStore(database)

    backup = store.last_migration_backup
    assert backup is not None
    assert backup.backup.is_file()
    assert backup.manifest.is_file()
    assert backup.source_runtime_schema == 0
    assert backup.target_runtime_schema == RUNTIME_SCHEMA_VERSION
    with closing(sqlite3.connect(backup.backup)) as connection:
        assert runtime_schema_version(connection) == 0
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
        assert str(connection.execute("PRAGMA integrity_check").fetchone()[0]).lower() == "ok"
    assert "sessions" not in _tables(backup.backup)
    manifest = json.loads(backup.manifest.read_text(encoding="utf-8"))
    assert Path(manifest["database"]) == database.resolve()
    assert Path(manifest["backup"]) == backup.backup.resolve()
    assert manifest["source_runtime_schema"] == 0
    assert manifest["target_runtime_schema"] == RUNTIME_SCHEMA_VERSION
    assert manifest["required_program_version"]


def test_fresh_database_does_not_create_a_migration_backup(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "fresh.db")

    assert store.last_migration_backup is None
    assert not (tmp_path / "backups").exists()


def test_migration_waits_for_writer_and_backup_includes_committed_wal_data(
    tmp_path: Path,
) -> None:
    database = _copy_legacy_database(tmp_path)
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?",
        ("writer-committed-before-migration", "legacy-running-task"),
    )
    started = threading.Event()
    finished = threading.Event()
    outcome: list[SQLiteStore | Exception] = []

    def migrate() -> None:
        started.set()
        try:
            outcome.append(SQLiteStore(database))
        except Exception as exc:
            outcome.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=migrate)
    worker.start()
    assert started.wait(timeout=2)
    assert not finished.wait(timeout=0.2)
    writer.commit()
    writer.close()
    assert finished.wait(timeout=15)
    worker.join(timeout=2)

    assert len(outcome) == 1
    assert isinstance(outcome[0], SQLiteStore)
    backup = outcome[0].last_migration_backup
    assert backup is not None
    with closing(sqlite3.connect(backup.backup)) as connection:
        updated_at = connection.execute(
            "SELECT updated_at FROM tasks WHERE id = 'legacy-running-task'"
        ).fetchone()[0]
    assert updated_at == "writer-committed-before-migration"


def test_failed_migration_keeps_backup_and_rolls_database_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _copy_legacy_database(tmp_path)

    def fail_mapping(_connection: sqlite3.Connection, _task: object) -> list[Effect]:
        raise RuntimeError("injected mapping failure")

    monkeypatch.setattr(patchloop.persistence, "_legacy_effects", fail_mapping)
    with pytest.raises(RuntimeSchemaError, match="injected mapping failure"):
        SQLiteStore(database)

    backups = list((tmp_path / "backups").glob("*.sqlite"))
    assert len(backups) == 1
    assert "sessions" not in _tables(database)
    with closing(sqlite3.connect(database)) as connection:
        assert runtime_schema_version(connection) == 0


def test_explicit_rollback_preserves_upgraded_data_before_restore(tmp_path: Path) -> None:
    database = _copy_legacy_database(tmp_path)
    store = SQLiteStore(database)
    backup = store.last_migration_backup
    assert backup is not None
    store.create_session(Session(id="post-upgrade-session", workspace_ref="workspace"))

    with pytest.raises(ValueError, match="writers to be stopped"):
        restore_migration_backup(database, backup.backup, writers_stopped=False)

    result = restore_migration_backup(database, backup.backup, writers_stopped=True)

    assert result.restored_runtime_schema == 0
    assert result.required_program_version
    assert result.preserved_upgraded_database.is_file()
    assert "sessions" not in _tables(database)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    with closing(sqlite3.connect(result.preserved_upgraded_database)) as connection:
        assert runtime_schema_version(connection) == RUNTIME_SCHEMA_VERSION
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = 'post-upgrade-session'"
            ).fetchone()[0]
            == 1
        )
