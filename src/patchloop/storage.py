"""Small local task store used before the SQLite milestone."""

from pathlib import Path

from patchloop.domain import Task


class TaskNotFoundError(LookupError):
    pass


class JsonTaskStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, task: Task) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{task.id}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(task.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)

    def get(self, task_id: str) -> Task:
        path = self.root / f"{task_id}.json"
        if not path.is_file():
            raise TaskNotFoundError(task_id)
        return Task.model_validate_json(path.read_text(encoding="utf-8"))
