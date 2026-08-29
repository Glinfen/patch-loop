"""In-memory workspace change tracking and unified diff generation."""

from __future__ import annotations

import difflib
from pathlib import Path


class FileChangeTracker:
    def __init__(self, repository: Path) -> None:
        self.repository = repository
        self._original: dict[Path, str | None] = {}

    def capture(self, path: Path) -> None:
        if path not in self._original:
            self._original[path] = path.read_text(encoding="utf-8") if path.exists() else None

    def changed_paths(self) -> list[str]:
        return [
            path.relative_to(self.repository).as_posix()
            for path, before in self._original.items()
            if self._current(path) != before
        ]

    def diff(self) -> str:
        sections: list[str] = []
        for path in sorted(self._original):
            before = self._original[path]
            after = self._current(path)
            if before == after:
                continue
            relative = path.relative_to(self.repository).as_posix()
            sections.extend(
                difflib.unified_diff(
                    [] if before is None else before.splitlines(keepends=True),
                    [] if after is None else after.splitlines(keepends=True),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                )
            )
        return "".join(sections)

    @staticmethod
    def _current(path: Path) -> str | None:
        return path.read_text(encoding="utf-8") if path.exists() else None
