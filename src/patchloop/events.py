"""Append-only JSONL event trace."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from pydantic import BaseModel, Field


class Event(BaseModel):
    type: str
    task_id: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EventLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()

    def emit(self, event: Event) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(record + "\n")

    def read(self) -> list[Event]:
        if not self.path.exists():
            return []
        return [
            Event.model_validate_json(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
