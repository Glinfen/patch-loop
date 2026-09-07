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

    def capture_original(self, path: Path, content: str | None) -> None:
        """Restore a trusted persisted baseline without reading the current file."""

        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(self.repository)
        except ValueError as exc:
            raise ValueError(f"change baseline escapes repository: {path}") from exc
        self._original.setdefault(resolved, content)

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

    def snapshot(self) -> dict[str, str | None]:
        return {
            path.relative_to(self.repository).as_posix(): content
            for path, content in self._original.items()
        }

    def restore(self, snapshot: dict[str, str | None]) -> None:
        restored: dict[Path, str | None] = {}
        for relative, content in snapshot.items():
            path = (self.repository / relative).resolve(strict=False)
            try:
                path.relative_to(self.repository)
            except ValueError as exc:
                raise ValueError(f"change snapshot escapes repository: {relative}") from exc
            restored[path] = content
        self._original = restored

    @staticmethod
    def _current(path: Path) -> str | None:
        return path.read_text(encoding="utf-8") if path.exists() else None
