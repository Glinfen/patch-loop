from pathlib import Path

import pytest

from patchloop.domain import Task, TaskReport
from patchloop.storage import ArtifactStore, JsonTaskStore, TaskNotFoundError


def test_task_store_rejects_path_like_id(tmp_path: Path) -> None:
    store = JsonTaskStore(tmp_path)

    with pytest.raises(TaskNotFoundError):
        store.get("../outside")


def test_artifact_store_writes_report_and_diff(tmp_path: Path) -> None:
    task = Task(goal="Fix", repository=str(tmp_path))
    task.report = TaskReport(
        summary="Fixed",
        changed_files=["app.py"],
        diff="--- a/app.py\n+++ b/app.py\n",
    )

    paths = ArtifactStore(tmp_path / "artifacts").save_report(task)

    assert {path.name for path in paths} == {"report.json", "changes.diff"}
    assert "Fixed" in paths[0].read_text(encoding="utf-8")
