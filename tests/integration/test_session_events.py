"""SRF-02 step 5 authoritative journal and recoverable JSONL export."""

from __future__ import annotations

from pathlib import Path

import pytest

from patchloop.domain import Task
from patchloop.events import SessionEvent, SessionEventExporter
from patchloop.persistence import SQLiteStore
from patchloop.session.models import Session, Turn, TurnRole


def _store_with_events(tmp_path: Path) -> tuple[SQLiteStore, str]:
    store = SQLiteStore(tmp_path / "journal.db")
    session = store.create_session(Session(id="session-1", workspace_ref="workspace"))
    store.append_turn(
        Turn(
            id="turn-1",
            session_id=session.id,
            role=TurnRole.USER,
            content="Inspect the repository",
            client_submission_id="submission-1",
        )
    )
    store.start_task(
        session.id,
        Task(id="task-1", goal="Inspect", repository="workspace"),
        expected_version=store.get_session(session.id).version,
    )
    store.append_event(
        SessionEvent(
            id="event-custom",
            session_id=session.id,
            task_id="task-1",
            trace_id="trace-1",
            type="test.recorded",
            data={"value": "safe"},
        )
    )
    return store, session.id


def test_critical_session_changes_are_transactionally_journaled(tmp_path: Path) -> None:
    store, session_id = _store_with_events(tmp_path)

    events = store.list_events(session_id)
    assert [event.type for event in events] == [
        "session.created",
        "turn.appended",
        "task.started",
        "test.recorded",
    ]
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert len({event.id for event in events}) == len(events)
    assert events[1].data == {"turn_id": "turn-1", "role": "user"}
    assert events[2].task_id == "task-1"
    assert events[3].trace_id == "trace-1"
    assert store.get_session(session_id).event_sequence == events[-1].sequence


def test_export_is_repeatable_without_duplicate_or_missing_events(tmp_path: Path) -> None:
    store, session_id = _store_with_events(tmp_path)
    trace = tmp_path / "session.jsonl"
    exporter = SessionEventExporter(store)

    first = exporter.export(session_id, trace)
    second = exporter.export(session_id, trace)

    assert first.exported == 4
    assert second.exported == 0
    assert second.already_present == 4
    exported = [
        SessionEvent.model_validate_json(line)
        for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    assert exported == store.list_events(session_id)


def test_export_repairs_an_incomplete_tail_and_resumes(tmp_path: Path) -> None:
    store, session_id = _store_with_events(tmp_path)
    trace = tmp_path / "session.jsonl"
    exporter = SessionEventExporter(store)
    exporter.export(session_id, trace)
    expected = trace.read_bytes()
    trace.write_bytes(expected[:-13])

    result = exporter.export(session_id, trace)

    assert result.repaired_tail is True
    assert trace.read_bytes() == expected
    ids = [
        SessionEvent.model_validate_json(line).id
        for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    assert ids == [event.id for event in store.list_events(session_id)]


def test_export_rewrites_duplicate_or_out_of_order_projection(tmp_path: Path) -> None:
    store, session_id = _store_with_events(tmp_path)
    trace = tmp_path / "session.jsonl"
    events = store.list_events(session_id)
    trace.write_text(
        events[0].model_dump_json() + "\n" + events[0].model_dump_json() + "\n",
        encoding="utf-8",
    )

    result = SessionEventExporter(store).export(session_id, trace)

    assert result.rewritten is True
    exported = [
        SessionEvent.model_validate_json(line)
        for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    assert exported == events


def test_export_rejects_corruption_before_the_tail(tmp_path: Path) -> None:
    store, session_id = _store_with_events(tmp_path)
    trace = tmp_path / "session.jsonl"
    events = store.list_events(session_id)
    trace.write_text(
        events[0].model_dump_json() + "\nnot-json\n" + events[1].model_dump_json() + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="corrupt middle"):
        SessionEventExporter(store).export(session_id, trace)
