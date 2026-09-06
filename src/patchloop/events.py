"""Append-only JSONL event trace."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from patchloop.security import SecretRedactor


class Event(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str
    task_id: str
    trace_id: str | None = None
    sequence: int = Field(default=0, ge=0)
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SessionEvent(BaseModel):
    """Authoritative journal record for one Session state change.

    JSONL traces remain a projection of these records.  Session sequence is
    allocated by the store in the same transaction as the state change.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    session_id: str = Field(min_length=1)
    type: str = Field(min_length=1, max_length=256)
    task_id: str | None = Field(default=None, min_length=1)
    trace_id: str | None = Field(default=None, min_length=1)
    sequence: int = Field(default=0, ge=0)
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


def effect_commit_event_id(effect_id: str, version: int) -> str:
    """Return a stable, bounded journal ID for an Effect commit."""

    return journal_event_id("effect.committed", effect_id, version)


def journal_event_id(event_type: str, entity_id: str, revision: int | str) -> str:
    """Return a stable, bounded ID for a domain event."""

    digest = hashlib.sha256(f"{event_type}:{entity_id}:{revision}".encode()).hexdigest()
    return f"journal-{digest}"


class SessionEventSource(Protocol):
    def list_events(self, session_id: str, *, after_sequence: int = 0) -> list[SessionEvent]: ...


@dataclass(frozen=True)
class EventExportResult:
    session_id: str
    exported: int
    already_present: int
    repaired_tail: bool
    rewritten: bool


class SessionEventExporter:
    """Recoverably project one Session journal to JSONL."""

    def __init__(self, source: SessionEventSource, redactor: SecretRedactor | None = None) -> None:
        self.source = source
        self.redactor = redactor or SecretRedactor()

    def export(self, session_id: str, path: Path) -> EventExportResult:
        journal = self.source.list_events(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing, repaired_tail = self._read_complete_prefix(path, session_id)
        journal_ids = [event.id for event in journal]
        existing_ids = [event.id for event in existing]
        prefix_is_valid = (
            len(existing_ids) == len(set(existing_ids))
            and existing_ids == journal_ids[: len(existing_ids)]
            and all(left == right for left, right in zip(existing, journal, strict=False))
        )
        if not prefix_is_valid:
            self._replace(path, journal)
            return EventExportResult(session_id, len(journal), 0, repaired_tail, True)
        missing = journal[len(existing) :]
        if missing:
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                for event in missing:
                    stream.write(self._record(event) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
        elif not path.exists():
            path.touch()
        return EventExportResult(session_id, len(missing), len(existing), repaired_tail, False)

    def _read_complete_prefix(self, path: Path, session_id: str) -> tuple[list[SessionEvent], bool]:
        if not path.exists():
            return [], False
        raw = path.read_bytes()
        repaired = bool(raw and not raw.endswith(b"\n"))
        if repaired:
            boundary = raw.rfind(b"\n") + 1
            with path.open("r+b") as stream:
                stream.truncate(boundary)
                stream.flush()
                os.fsync(stream.fileno())
            raw = raw[:boundary]
        events: list[SessionEvent] = []
        lines = raw.decode("utf-8").splitlines()
        for index, line in enumerate(lines):
            try:
                event = SessionEvent.model_validate_json(line)
            except ValueError:
                if index != len(lines) - 1:
                    raise ValueError(
                        "session event export contains a corrupt middle record"
                    ) from None
                with path.open("r+b") as stream:
                    valid = "".join(self._record(item) + "\n" for item in events).encode()
                    stream.seek(0)
                    stream.write(valid)
                    stream.truncate()
                    stream.flush()
                    os.fsync(stream.fileno())
                return events, True
            if event.session_id != session_id:
                raise ValueError("session event export contains another session")
            events.append(event)
        return events, repaired

    def _replace(self, path: Path, events: list[SessionEvent]) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for event in events:
                stream.write(self._record(event) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _record(self, event: SessionEvent) -> str:
        payload = self.redactor.redact(event.model_dump(mode="json"))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class EventLogger:
    def __init__(self, path: Path, redactor: SecretRedactor | None = None) -> None:
        self.path = path
        self._lock = Lock()
        self.redactor = redactor or SecretRedactor()
        self._sequence = self._last_sequence()

    def _last_sequence(self) -> int:
        if not self.path.is_file():
            return 0
        last = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                last = max(last, Event.model_validate_json(line).sequence)
            except ValueError:
                continue
        return last

    def emit(self, event: Event) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            self._sequence += 1
            event.sequence = self._sequence
            event.trace_id = event.trace_id or event.task_id
            payload = self.redactor.redact(event.model_dump(mode="json"))
            record = json.dumps(payload, ensure_ascii=False)
            stream.write(record + "\n")

    def read(self) -> list[Event]:
        if not self.path.exists():
            return []
        return [
            Event.model_validate_json(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
