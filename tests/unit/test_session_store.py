"""SRF-02: session runtime schema, store contracts, and Task projections.

The schema tests pin tables, foreign keys, and unique constraints. The
contract tests run the same behavioral suite against both the SRF-01
FakeStore and the SQLiteStore so the two implementations cannot drift, and
the SQLite-only tests verify real transaction semantics with concurrent
connections.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from patchloop.domain import Task, TaskRuntimeCondition, TaskStatus
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import (
    FakeStore,
    LeaseConflict,
    StaleVersion,
    SubmissionConflict,
)
from patchloop.session.models import Session, Turn, TurnRole
from patchloop.sqlite_support import connect

SESSION_RUNTIME_TABLES = {
    "sessions",
    "turns",
    "executions",
    "effects",
    "approvals",
    "control_requests",
    "recovery_dispositions",
    "session_events",
    "workspace_leases",
}

TASK_PROJECTION_COLUMNS = {"session_id", "outcome", "runtime_condition", "version"}


@pytest.fixture(params=["fake", "sqlite"])
def session_store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[object]:
    if request.param == "fake":
        yield FakeStore()
    else:
        yield SQLiteStore(tmp_path / "contract.db")


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "patchloop.db"
    SQLiteStore(database)
    return database


def _table_names(database: Path) -> set[str]:
    with closing(sqlite3.connect(database)) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def _table_columns(database: Path, table: str) -> set[str]:
    with closing(sqlite3.connect(database)) as connection:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _task_projection(database: Path, task_id: str) -> tuple[str | None, str, str, int]:
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            """
            SELECT session_id, outcome, runtime_condition, version
            FROM tasks WHERE id = ?
            """,
            (task_id,),
        ).fetchone()
    assert row is not None
    return (row[0], str(row[1]), str(row[2]), int(row[3]))


def _insert_session(database: Path, session_id: str = "session-1") -> None:
    session = Session(id=session_id, workspace_ref="workspace")
    with connect(database) as connection:
        connection.execute(
            """
            INSERT INTO sessions (
                id, workspace_ref, status, active_task_id, config_version,
                version, event_sequence, created_at, updated_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.workspace_ref,
                session.status.value,
                session.active_task_id,
                session.config_version,
                session.version,
                session.event_sequence,
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
                session.model_dump_json(),
            ),
        )


def _insert_turn(
    database: Path,
    *,
    sequence: int,
    session_id: str = "session-1",
    turn_id: str | None = None,
    client_submission_id: str | None = None,
) -> None:
    turn = Turn(
        id=turn_id or f"turn-{sequence}",
        session_id=session_id,
        role=TurnRole.USER,
        content=f"message {sequence}",
        sequence=sequence,
        client_submission_id=client_submission_id,
    )
    with connect(database) as connection:
        connection.execute(
            """
            INSERT INTO turns (
                id, session_id, task_id, sequence, client_submission_id,
                created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                turn.id,
                turn.session_id,
                turn.task_id,
                turn.sequence,
                turn.client_submission_id,
                turn.created_at.isoformat(),
                turn.model_dump_json(),
            ),
        )


def _insert_task(database: Path, task_id: str = "task-1") -> None:
    SQLiteStore(database).save_task(Task(id=task_id, goal="Inspect", repository="workspace"))


def _insert_effect(
    database: Path,
    effect_id: str,
    *,
    task_id: str = "task-1",
    step_id: str = "step-1",
    batch_position: int = 0,
) -> None:
    now = datetime.now(UTC)
    with connect(database) as connection:
        connection.execute(
            """
            INSERT INTO effects (
                id, task_id, step_id, batch_position, provider_call_id,
                retry_of_effect_id, status, approval_id, version,
                created_at, updated_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                effect_id,
                task_id,
                step_id,
                batch_position,
                f"provider-{effect_id}",
                None,
                "prepared",
                None,
                1,
                now.isoformat(),
                now.isoformat(),
                "{}",
            ),
        )


def _insert_approval(database: Path, approval_id: str, *, effect_id: str) -> None:
    with connect(database) as connection:
        connection.execute(
            """
            INSERT INTO approvals (
                id, effect_id, status, decision_source, decided_at,
                version, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval_id,
                effect_id,
                "pending",
                None,
                None,
                1,
                datetime.now(UTC).isoformat(),
                "{}",
            ),
        )


def _insert_disposition(database: Path, disposition_id: str, *, effect_id: str) -> None:
    with connect(database) as connection:
        connection.execute(
            """
            INSERT INTO recovery_dispositions (
                id, unknown_effect_id, kind, retry_effect_id, decision_source,
                created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                disposition_id,
                effect_id,
                "abandon",
                None,
                "operator",
                datetime.now(UTC).isoformat(),
                "{}",
            ),
        )


def _insert_execution(
    database: Path,
    execution_id: str = "execution-1",
    *,
    session_id: str = "session-1",
    task_id: str = "task-1",
) -> None:
    now = datetime.now(UTC)
    with connect(database) as connection:
        connection.execute(
            """
            INSERT INTO executions (
                id, session_id, task_id, owner_id, lease_token, generation,
                lease_expires_at, status, version, created_at, updated_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                session_id,
                task_id,
                "worker-1",
                "token-1",
                1,
                (now + timedelta(minutes=1)).isoformat(),
                "claimed",
                1,
                now.isoformat(),
                now.isoformat(),
                "{}",
            ),
        )


def test_fresh_store_creates_session_runtime_schema(tmp_path: Path) -> None:
    database = _database(tmp_path)

    assert SESSION_RUNTIME_TABLES.issubset(_table_names(database))
    assert TASK_PROJECTION_COLUMNS.issubset(_table_columns(database, "tasks"))


def test_save_task_persists_projection_columns(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = SQLiteStore(database)
    task = Task(id="task-1", goal="Inspect", repository="workspace")
    task.transition_runtime(TaskRuntimeCondition.RUNNING)
    task.transition_runtime(TaskRuntimeCondition.WAITING_FOR_APPROVAL)
    task = task.model_copy(update={"version": 7})

    store.save_task(task)

    assert _task_projection(database, "task-1") == (None, "active", "waiting_for_approval", 7)


def test_save_task_persists_session_association(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_session(database)
    SQLiteStore(database).save_task(
        Task(id="task-1", goal="Inspect", repository="workspace", session_id="session-1")
    )

    assert _task_projection(database, "task-1")[0] == "session-1"


def test_save_task_rejects_unknown_session_reference(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = SQLiteStore(database)
    task = Task(id="task-1", goal="Inspect", repository="workspace", session_id="missing")

    with pytest.raises(sqlite3.IntegrityError):
        store.save_task(task)


def test_turn_sequence_is_unique_within_session(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_session(database)
    _insert_turn(database, sequence=1)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_turn(database, sequence=1, turn_id="turn-duplicate")

    _insert_turn(database, sequence=2, turn_id="turn-2")
    _insert_session(database, session_id="session-2")
    _insert_turn(database, sequence=1, session_id="session-2", turn_id="turn-other-session")


def test_turn_client_submission_id_is_unique_within_session(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_session(database)
    _insert_turn(database, sequence=1, client_submission_id="client-1")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_turn(database, sequence=2, turn_id="turn-2", client_submission_id="client-1")

    # NULL submission ids are unconstrained, and other sessions may reuse ids.
    _insert_turn(database, sequence=3, turn_id="turn-3")
    _insert_turn(database, sequence=4, turn_id="turn-4")
    _insert_session(database, session_id="session-2")
    _insert_turn(
        database,
        sequence=1,
        session_id="session-2",
        turn_id="turn-5",
        client_submission_id="client-1",
    )


def test_turns_require_existing_session(tmp_path: Path) -> None:
    database = _database(tmp_path)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_turn(database, sequence=1, session_id="missing-session")


def test_session_events_sequence_is_unique_within_session(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_session(database)
    with connect(database) as connection:
        connection.executemany(
            """
            INSERT INTO session_events (
                id, session_id, sequence, type, task_id, trace_id, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "event-1",
                    "session-1",
                    1,
                    "session.created",
                    None,
                    None,
                    "2026-01-01T00:00:00Z",
                    "{}",
                ),
                (
                    "event-2",
                    "session-1",
                    2,
                    "turn.appended",
                    None,
                    None,
                    "2026-01-01T00:00:01Z",
                    "{}",
                ),
            ],
        )

    with pytest.raises(sqlite3.IntegrityError), connect(database) as connection:
        connection.execute(
            """
            INSERT INTO session_events (
                id, session_id, sequence, type, task_id, trace_id, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("event-3", "session-1", 2, "turn.appended", None, None, "2026-01-01T00:00:02Z", "{}"),
        )


def test_effect_batch_position_is_unique_per_task_and_step(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_task(database)
    _insert_effect(database, "effect-1")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_effect(database, "effect-2")

    _insert_effect(database, "effect-3", batch_position=1)
    _insert_effect(database, "effect-4", step_id="step-2")


def test_approval_is_unique_per_effect(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_task(database)
    _insert_effect(database, "effect-1")
    _insert_approval(database, "approval-1", effect_id="effect-1")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_approval(database, "approval-2", effect_id="effect-1")


def test_recovery_disposition_is_unique_per_unknown_effect(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_task(database)
    _insert_effect(database, "effect-1")
    _insert_disposition(database, "disposition-1", effect_id="effect-1")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_disposition(database, "disposition-2", effect_id="effect-1")


def test_executions_require_existing_session_and_task(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_session(database)
    _insert_task(database)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_execution(database, "execution-missing-session", session_id="missing")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_execution(database, "execution-missing-task", task_id="missing")

    _insert_execution(database)


def test_deleting_task_clears_active_session_reference(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_session(database)
    store = SQLiteStore(database)
    store.save_task(
        Task(id="task-1", goal="Inspect", repository="workspace", session_id="session-1")
    )
    with connect(database) as connection:
        connection.execute("UPDATE sessions SET active_task_id = 'task-1' WHERE id = 'session-1'")

    store.delete_task("task-1")

    with closing(sqlite3.connect(database)) as connection:
        active_task_id = connection.execute(
            "SELECT active_task_id FROM sessions WHERE id = 'session-1'"
        ).fetchone()[0]
    assert active_task_id is None


def test_deleting_task_cascades_session_runtime_children(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_session(database)
    store = SQLiteStore(database)
    store.save_task(
        Task(id="task-1", goal="Inspect", repository="workspace", session_id="session-1")
    )
    _insert_turn(database, sequence=1)
    _insert_execution(database)
    _insert_effect(database, "effect-1")
    _insert_approval(database, "approval-1", effect_id="effect-1")
    _insert_disposition(database, "disposition-1", effect_id="effect-1")
    with connect(database) as connection:
        connection.execute(
            """
            INSERT INTO control_requests (
                id, task_id, execution_id, kind, status, version, requested_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "control-1",
                "task-1",
                "execution-1",
                "pause",
                "requested",
                1,
                "2026-01-01T00:00:00Z",
                "{}",
            ),
        )
        connection.execute("UPDATE sessions SET active_task_id = 'task-1' WHERE id = 'session-1'")

    store.delete_task("task-1")

    with closing(sqlite3.connect(database)) as connection:
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "executions",
                "effects",
                "approvals",
                "recovery_dispositions",
                "control_requests",
            )
        }
        turn_task_id = connection.execute(
            "SELECT task_id FROM turns WHERE id = 'turn-1'"
        ).fetchone()[0]
    assert counts == {
        "executions": 0,
        "effects": 0,
        "approvals": 0,
        "recovery_dispositions": 0,
        "control_requests": 0,
    }
    assert turn_task_id is None


# -- Shared store contracts: the same suite runs against FakeStore and SQLiteStore --


def test_create_session_round_trip_and_duplicate_rejection(session_store: object) -> None:
    store = session_store
    created = store.create_session(Session(id="session-1", workspace_ref="workspace"))

    assert created.id == "session-1"
    assert store.get_session("session-1").workspace_ref == "workspace"
    assert store.get_session("session-1").version == 1
    with pytest.raises(ValueError, match="already exists"):
        store.create_session(Session(id="session-1", workspace_ref="other"))


def test_get_missing_session_raises_key_error(session_store: object) -> None:
    with pytest.raises(KeyError):
        session_store.get_session("missing-session")


def test_list_sessions_filters_by_workspace(session_store: object) -> None:
    store = session_store
    store.create_session(Session(id="session-1", workspace_ref="workspace-a"))
    store.create_session(Session(id="session-2", workspace_ref="workspace-b"))
    store.create_session(Session(id="session-3", workspace_ref="workspace-a"))

    assert sorted(s.id for s in store.list_sessions("workspace-a")) == [
        "session-1",
        "session-3",
    ]
    assert sorted(s.id for s in store.list_sessions()) == [
        "session-1",
        "session-2",
        "session-3",
    ]


def test_close_session_requires_version_and_free_active_slot(session_store: object) -> None:
    store = session_store
    session = store.create_session(Session(id="session-1", workspace_ref="workspace"))

    with pytest.raises(StaleVersion):
        store.close_session("session-1", expected_version=session.version + 5)

    closed = store.close_session("session-1", expected_version=session.version)
    assert closed.status.value == "closed"
    assert closed.version == session.version + 1
    assert store.get_session("session-1").version == closed.version

    occupied = store.create_session(Session(id="session-2", workspace_ref="workspace"))
    store.start_task(
        "session-2",
        Task(id="task-1", goal="Inspect", repository="workspace"),
        expected_version=occupied.version,
    )
    with pytest.raises(ValueError, match="active task"):
        store.close_session("session-2", expected_version=store.get_session("session-2").version)


def test_append_turn_assigns_sequence_and_binds_active_task(session_store: object) -> None:
    store = session_store
    store.create_session(Session(id="session-1", workspace_ref="workspace"))

    first = store.append_turn(Turn(session_id="session-1", role=TurnRole.USER, content="hello"))
    assert first.sequence == 1
    assert first.task_id is None

    store.start_task(
        "session-1",
        Task(id="task-1", goal="Inspect", repository="workspace"),
        expected_version=store.get_session("session-1").version,
    )
    second = store.append_turn(
        Turn(session_id="session-1", role=TurnRole.USER, content="more context")
    )
    assert second.sequence == 2
    assert second.task_id == "task-1"


def test_append_turn_requires_existing_session(session_store: object) -> None:
    with pytest.raises(KeyError):
        session_store.append_turn(
            Turn(session_id="missing-session", role=TurnRole.USER, content="hello")
        )


def test_append_turn_deduplicates_client_submissions(session_store: object) -> None:
    store = session_store
    store.create_session(Session(id="session-1", workspace_ref="workspace"))
    first = store.append_turn(
        Turn(
            session_id="session-1",
            role=TurnRole.USER,
            content="hello",
            client_submission_id="client-1",
        )
    )

    again = store.append_turn(
        Turn(
            session_id="session-1",
            role=TurnRole.USER,
            content="hello",
            client_submission_id="client-1",
        )
    )

    assert again.id == first.id
    assert again.sequence == first.sequence
    assert len(store.list_turns("session-1")) == 1


def test_append_turn_conflicts_on_same_submission_different_content(
    session_store: object,
) -> None:
    store = session_store
    store.create_session(Session(id="session-1", workspace_ref="workspace"))
    store.append_turn(
        Turn(
            session_id="session-1",
            role=TurnRole.USER,
            content="hello",
            client_submission_id="client-1",
        )
    )

    with pytest.raises(SubmissionConflict):
        store.append_turn(
            Turn(
                session_id="session-1",
                role=TurnRole.USER,
                content="different",
                client_submission_id="client-1",
            )
        )


def test_append_turn_checks_expected_version(session_store: object) -> None:
    store = session_store
    store.create_session(Session(id="session-1", workspace_ref="workspace"))
    store.append_turn(Turn(session_id="session-1", role=TurnRole.USER, content="hello"))

    with pytest.raises(StaleVersion):
        store.append_turn(
            Turn(session_id="session-1", role=TurnRole.USER, content="again"),
            expected_version=1,
        )


def test_list_turns_pages_after_sequence(session_store: object) -> None:
    store = session_store
    store.create_session(Session(id="session-1", workspace_ref="workspace"))
    for index in range(4):
        store.append_turn(
            Turn(session_id="session-1", role=TurnRole.USER, content=f"message {index}")
        )

    assert [turn.sequence for turn in store.list_turns("session-1")] == [1, 2, 3, 4]
    assert [turn.sequence for turn in store.list_turns("session-1", after_sequence=2)] == [3, 4]
    assert store.list_turns("session-1", after_sequence=4) == []
    with pytest.raises(KeyError):
        store.list_turns("missing-session")


def test_start_task_binds_active_slot_once(session_store: object) -> None:
    store = session_store
    session = store.create_session(Session(id="session-1", workspace_ref="workspace"))

    task = store.start_task(
        "session-1",
        Task(id="task-1", goal="Inspect", repository="workspace"),
        expected_version=session.version,
    )

    assert task.session_id == "session-1"
    updated = store.get_session("session-1")
    assert updated.active_task_id == "task-1"
    assert updated.version == session.version + 1
    with pytest.raises(LeaseConflict):
        store.start_task(
            "session-1",
            Task(id="task-2", goal="Second", repository="workspace"),
            expected_version=updated.version,
        )


def test_start_task_rejects_closed_sessions_and_terminal_tasks(session_store: object) -> None:
    store = session_store
    session = store.create_session(Session(id="session-1", workspace_ref="workspace"))
    closed = store.close_session("session-1", expected_version=session.version)

    with pytest.raises(ValueError, match="closed session"):
        store.start_task(
            "session-1",
            Task(id="task-1", goal="Inspect", repository="workspace"),
            expected_version=closed.version,
        )

    open_session = store.create_session(Session(id="session-2", workspace_ref="workspace"))
    terminal = Task(id="task-x", goal="Done", repository="workspace", status=TaskStatus.COMPLETED)
    with pytest.raises(ValueError, match="active outcome"):
        store.start_task("session-2", terminal, expected_version=open_session.version)
    with pytest.raises(StaleVersion):
        store.start_task(
            "session-2",
            Task(id="task-3", goal="Inspect", repository="workspace"),
            expected_version=open_session.version + 41,
        )


# -- Real SQLite transaction semantics --


def _run_threads(workers: list[threading.Thread], timeout: float = 30.0) -> None:
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=timeout)
    assert not any(worker.is_alive() for worker in workers)


def test_concurrent_same_submission_creates_single_turn(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = SQLiteStore(database)
    store.create_session(Session(id="session-1", workspace_ref="workspace"))
    barrier = threading.Barrier(2)
    outcomes: list[Turn] = []
    errors: list[Exception] = []

    def submit() -> None:
        worker = SQLiteStore(database)
        barrier.wait()
        try:
            outcomes.append(
                worker.append_turn(
                    Turn(
                        session_id="session-1",
                        role=TurnRole.USER,
                        content="same message",
                        client_submission_id="client-1",
                    )
                )
            )
        except Exception as exc:
            errors.append(exc)

    _run_threads([threading.Thread(target=submit) for _ in range(2)])

    assert errors == []
    assert len(outcomes) == 2
    assert outcomes[0].id == outcomes[1].id
    assert outcomes[0].sequence == 1
    assert len(store.list_turns("session-1")) == 1


def test_concurrent_distinct_messages_get_unique_ordered_sequences(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = SQLiteStore(database)
    store.create_session(Session(id="session-1", workspace_ref="workspace"))
    barrier = threading.Barrier(6)
    errors: list[Exception] = []

    def submit(worker: int) -> None:
        worker_store = SQLiteStore(database)
        barrier.wait()
        try:
            worker_store.append_turn(
                Turn(
                    session_id="session-1",
                    role=TurnRole.USER,
                    content=f"message {worker}",
                    client_submission_id=f"client-{worker}",
                )
            )
        except Exception as exc:
            errors.append(exc)

    _run_threads([threading.Thread(target=submit, args=(worker,)) for worker in range(6)])

    turns = store.list_turns("session-1")
    assert errors == []
    assert [turn.sequence for turn in turns] == [1, 2, 3, 4, 5, 6]
    assert len({turn.id for turn in turns}) == 6
    assert store.get_session("session-1").version == 7
