"""Append-only JSONL event trace."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from patchloop.security import SecretRedactor


class Event(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str
    task_id: str
    trace_id: str | None = None
    sequence: int = Field(default=0, ge=0)
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


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
