from pathlib import Path

import pytest

from patchloop.domain import AgentStep, StepStatus, Task, TaskReport, ToolCall, ToolResult
from patchloop.persistence import RuntimeCheckpoint, SQLiteStore
from patchloop.providers import ModelMessage
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


def test_sqlite_store_round_trips_runtime_state(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "patchloop.db")
    task = Task(goal="Persist", repository=str(tmp_path))
    store.save_task(task)
    step = AgentStep(task_id=task.id, index=0, status=StepStatus.COMPLETED)
    store.record_step(step)
    call = ToolCall(id="call-1", name="list_files")
    result = ToolResult(call_id=call.id, tool_name=call.name, success=True, output="app.py")
    store.record_tool_call(task.id, call, result)
    checkpoint = RuntimeCheckpoint(
        task_id=task.id,
        next_step_index=1,
        messages=[ModelMessage(role="user", content="Persist")],
        tool_history=[result],
        cache_hit_tokens=80,
        cache_miss_tokens=20,
        cache_write_tokens=5,
        cache_usage_reported_calls=2,
        cache_usage_unreported_calls=1,
        cache_usage_inconsistent_calls=1,
        cache_write_reported_calls=1,
    )
    store.save_checkpoint(checkpoint)
    artifact = tmp_path / "report.json"
    artifact.write_text("{}", encoding="utf-8")
    store.record_artifact(task.id, artifact)

    assert store.get_task(task.id).goal == "Persist"
    assert store.list_steps(task.id)[0].status is StepStatus.COMPLETED
    assert store.get_tool_result(task.id, call.id) == result
    assert store.list_tool_results(task.id) == [result]
    restored_checkpoint = store.get_checkpoint(task.id)
    assert restored_checkpoint.next_step_index == 1
    assert restored_checkpoint.cache_hit_tokens == 80
    assert restored_checkpoint.cache_miss_tokens == 20
    assert restored_checkpoint.cache_write_tokens == 5
    assert restored_checkpoint.cache_usage_reported_calls == 2
    assert restored_checkpoint.cache_usage_unreported_calls == 1
    assert restored_checkpoint.cache_usage_inconsistent_calls == 1
    assert restored_checkpoint.cache_write_reported_calls == 1
    assert store.list_artifacts(task.id) == [artifact]


def test_sqlite_tool_call_ids_are_scoped_to_task(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "patchloop.db")
    first_task = Task(goal="First", repository=str(tmp_path))
    second_task = Task(goal="Second", repository=str(tmp_path))
    store.save_task(first_task)
    store.save_task(second_task)
    call = ToolCall(id="shared-call", name="list_files")
    first_result = ToolResult(
        call_id=call.id,
        tool_name=call.name,
        success=True,
        output="first.py",
    )
    second_result = ToolResult(
        call_id=call.id,
        tool_name=call.name,
        success=True,
        output="second.py",
    )

    store.record_tool_call(first_task.id, call, first_result)
    store.record_tool_call(second_task.id, call, second_result)

    assert store.get_tool_result(first_task.id, call.id) == first_result
    assert store.get_tool_result(second_task.id, call.id) == second_result
