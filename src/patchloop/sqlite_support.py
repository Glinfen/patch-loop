"""Centralized SQLite connection entry for PatchLoop stores.

Every store opens its database through :func:`connect` so WAL journaling,
foreign keys, and the busy timeout are configured and verified in one place
instead of being re-implemented per store.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from os import fsync, replace
from pathlib import Path
from uuid import uuid4

BUSY_TIMEOUT_SECONDS = 10.0


class SQLiteConfigurationError(RuntimeError):
    """Raised when a connection cannot provide the required SQLite guarantees."""


@dataclass(frozen=True)
class MigrationBackup:
    database: Path
    backup: Path
    manifest: Path
    source_runtime_schema: int
    target_runtime_schema: int


@dataclass(frozen=True)
class RollbackResult:
    database: Path
    restored_backup: Path
    preserved_upgraded_database: Path
    restored_runtime_schema: int
    required_program_version: str


def open_connection(path: Path | str) -> sqlite3.Connection:
    """Open the one configured connection entry used by every store."""

    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    verify_connection_config(connection)
    return connection


def verify_connection_config(connection: sqlite3.Connection) -> None:
    """Read back the actual connection configuration and reject mismatches."""

    journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    busy_timeout_ms = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    expected_timeout_ms = round(BUSY_TIMEOUT_SECONDS * 1000)
    problems: list[str] = []
    if journal_mode != "wal":
        problems.append(f"journal_mode={journal_mode}")
    if foreign_keys != 1:
        problems.append(f"foreign_keys={foreign_keys}")
    if busy_timeout_ms != expected_timeout_ms:
        problems.append(f"busy_timeout_ms={busy_timeout_ms} expected={expected_timeout_ms}")
    if problems:
        raise SQLiteConfigurationError(
            "SQLite connection configuration rejected: " + ", ".join(problems)
        )


@contextmanager
def connect(path: Path | str) -> Iterator[sqlite3.Connection]:
    """Yield one configured connection; commit on success, roll back on failure."""

    connection = open_connection(path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def connect_write(path: Path | str) -> Iterator[sqlite3.Connection]:
    """Yield one configured connection in an immediate write transaction.

    ``BEGIN IMMEDIATE`` takes the database write lock up front so concurrent
    writers serialize on the busy timeout; a check-then-write sequence inside
    the transaction cannot interleave with another writer.
    """

    connection = open_connection(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def runtime_schema_version(connection: sqlite3.Connection) -> int:
    table = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'patchloop_schema_migrations'
        """
    ).fetchone()
    if table is None:
        return 0
    row = connection.execute(
        "SELECT version FROM patchloop_schema_migrations WHERE component = 'runtime'"
    ).fetchone()
    return 0 if row is None else int(row[0])


def has_runtime_data(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).fetchone()
        is not None
    )


def create_migration_backup(
    connection: sqlite3.Connection,
    database: Path,
    *,
    source_runtime_schema: int,
    target_runtime_schema: int,
) -> MigrationBackup:
    """Create a consistent snapshot while the caller holds a separate writer lock."""

    backup_dir = database.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_dir / (
        f"{database.stem}.pre-runtime-v{target_runtime_schema}.{stamp}.{uuid4().hex}.sqlite"
    )
    with sqlite3.connect(backup) as destination:
        connection.backup(destination)
        if str(destination.execute("PRAGMA integrity_check").fetchone()[0]).lower() != "ok":
            raise SQLiteConfigurationError("migration backup failed integrity_check")
    manifest = backup.with_suffix(backup.suffix + ".json")
    payload = {
        "schema_version": 1,
        "database": str(database.resolve()),
        "backup": str(backup.resolve()),
        "source_runtime_schema": source_runtime_schema,
        "target_runtime_schema": target_runtime_schema,
        "required_program_version": _program_version(),
        "created_at": datetime.now(UTC).isoformat(),
    }
    temporary = manifest.with_suffix(manifest.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        fsync(stream.fileno())
    replace(temporary, manifest)
    return MigrationBackup(
        database=database,
        backup=backup,
        manifest=manifest,
        source_runtime_schema=source_runtime_schema,
        target_runtime_schema=target_runtime_schema,
    )


def restore_migration_backup(
    database: Path,
    backup: Path,
    *,
    writers_stopped: bool,
) -> RollbackResult:
    """Restore a pre-migration snapshot after preserving the upgraded database."""

    if not writers_stopped:
        raise ValueError("rollback requires execution and writers to be stopped")
    database = database.resolve()
    backup = backup.resolve()
    if database == backup or not database.is_file() or not backup.is_file():
        raise ValueError("rollback database and backup must be distinct existing files")
    manifest_path = backup.with_suffix(backup.suffix + ".json")
    if not manifest_path.is_file():
        raise ValueError("migration backup manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if Path(str(manifest.get("database", ""))).resolve() != database:
        raise ValueError("migration backup belongs to another database")
    if Path(str(manifest.get("backup", ""))).resolve() != backup:
        raise ValueError("migration backup manifest path does not match")
    restored_schema = int(manifest["source_runtime_schema"])
    current_schema = _database_runtime_schema(database)
    if current_schema <= restored_schema:
        raise ValueError("rollback backup is not older than the current database")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    preserved = (
        database.parent
        / "backups"
        / (f"{database.stem}.upgraded-runtime-v{current_schema}.{stamp}.{uuid4().hex}.sqlite")
    )
    preserved.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database, timeout=BUSY_TIMEOUT_SECONDS) as current:
        current.execute("PRAGMA busy_timeout = 10000")
        current.execute("BEGIN EXCLUSIVE")
        with (
            sqlite3.connect(database) as snapshot_source,
            sqlite3.connect(preserved) as destination,
        ):
            snapshot_source.backup(destination)
        current.commit()
    with (
        sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True) as source,
        sqlite3.connect(database, timeout=BUSY_TIMEOUT_SECONDS) as destination,
    ):
        source.backup(destination)
        result = str(destination.execute("PRAGMA integrity_check").fetchone()[0]).lower()
        if result != "ok":
            raise SQLiteConfigurationError("restored database failed integrity_check")
    return RollbackResult(
        database=database,
        restored_backup=backup,
        preserved_upgraded_database=preserved,
        restored_runtime_schema=restored_schema,
        required_program_version=str(manifest["required_program_version"]),
    )


def _database_runtime_schema(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        return runtime_schema_version(connection)


def _program_version() -> str:
    try:
        return version("patchloop")
    except PackageNotFoundError:
        return "0.1.0"
