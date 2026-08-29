"""Small local task store used before the SQLite milestone."""

import json
import re
from pathlib import Path

from patchloop.domain import Task
from patchloop.security import SecretRedactor


class TaskNotFoundError(LookupError):
    pass


class JsonTaskStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, task: Task) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._task_path(task.id)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(task.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)

    def get(self, task_id: str) -> Task:
        path = self._task_path(task_id)
        if not path.is_file():
            raise TaskNotFoundError(task_id)
        return Task.model_validate_json(path.read_text(encoding="utf-8"))

    def _task_path(self, task_id: str) -> Path:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", task_id) is None:
            raise TaskNotFoundError(task_id)
        return self.root / f"{task_id}.json"


class ArtifactStore:
    def __init__(self, root: Path, redactor: SecretRedactor | None = None) -> None:
        self.root = root
        self.redactor = redactor or SecretRedactor()

    def save_report(self, task: Task) -> list[Path]:
        if task.report is None:
            raise ValueError("task does not have a report")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", task.id) is None:
            raise ValueError("invalid task id")
        task_root = self.root / task.id
        task_root.mkdir(parents=True, exist_ok=True)
        report_path = task_root / "report.json"
        self._atomic_write(
            report_path,
            json.dumps(
                self.redactor.redact(task.report.model_dump(mode="json")),
                ensure_ascii=False,
                indent=2,
            ),
        )
        paths = [report_path]
        if task.report.diff:
            diff_path = task_root / "changes.diff"
            self._atomic_write(diff_path, self.redactor.redact_text(task.report.diff))
            paths.append(diff_path)
        return paths

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
