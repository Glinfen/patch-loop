import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from patchloop.domain import (
    AgentStep,
    PromptCacheLayout,
    StepStatus,
    Task,
    TaskExecutionConfig,
    TaskReport,
    ToolCall,
    ToolResult,
)
from patchloop.execution.models import Execution
from patchloop.persistence import (
    CheckpointSchemaError,
    RuntimeCheckpoint,
    SQLiteStore,
    _decode_task_payload,
)
from patchloop.persistence_contracts import LeaseGuard
from patchloop.prompt_cache import AppendOnlyPromptState, CacheEpoch, PromptCacheCoordinator
from patchloop.providers import ModelMessage
from patchloop.session.models import Session, SessionCheckpoint
from patchloop.sqlite_support import connect_write
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
    epoch = CacheEpoch.bootstrap(
        [
            ModelMessage(role="system", content="static"),
            ModelMessage(role="user", content="Persist"),
        ],
        prefix_message_count=2,
        epoch_id="initial",
    )
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
        cache_epoch_state=epoch.snapshot,
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
    assert restored_checkpoint.cache_epoch_state is not None
    assert restored_checkpoint.cache_epoch_state.prefix_fingerprint == (
        epoch.snapshot.prefix_fingerprint
    )
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


def test_checkpoint_adapter_accepts_legacy_payload_without_version(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "patchloop.db")
    task = Task(id="task-1", goal="Resume", repository=str(tmp_path))
    store.save_task(task)
    payload = RuntimeCheckpoint(task_id=task.id, next_step_index=0, messages=[]).model_dump(
        mode="json"
    )
    payload.pop("schema_version")
    with connect_write(store.path) as connection:
        connection.execute(
            "INSERT INTO checkpoints (task_id, payload_json, updated_at) VALUES (?, ?, ?)",
            (task.id, json.dumps(payload), payload["updated_at"]),
        )

    assert store.get_checkpoint(task.id).schema_version == "1.0"


def test_checkpoint_adapter_rejects_unknown_schema(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "patchloop.db")
    task = Task(id="task-1", goal="Resume", repository=str(tmp_path))
    store.save_task(task)
    payload = RuntimeCheckpoint(task_id=task.id, next_step_index=0, messages=[]).model_dump(
        mode="json"
    )
    payload["schema_version"] = "2.0"
    with connect_write(store.path) as connection:
        connection.execute(
            "INSERT INTO checkpoints (task_id, payload_json, updated_at) VALUES (?, ?, ?)",
            (task.id, json.dumps(payload), payload["updated_at"]),
        )

    with pytest.raises(CheckpointSchemaError, match="not supported"):
        store.get_checkpoint(task.id)


def _append_only_state() -> AppendOnlyPromptState:
    return AppendOnlyPromptState(
        root_prefix_message_count=2,
        last_submitted_message_count=2,
        last_submitted_message_fingerprints=["a" * 64, "b" * 64],
        last_submitted_request_id="request-1",
        last_submitted_tool_fingerprint="c" * 64,
        epoch_generation=1,
    )


def _append_only_coordinator() -> PromptCacheCoordinator:
    messages = [
        ModelMessage(role="system", content="static"),
        ModelMessage(role="user", content="Append"),
        ModelMessage(role="assistant", content="inspected"),
    ]
    epoch = CacheEpoch.bootstrap(
        messages,
        prefix_message_count=2,
        epoch_id="initial",
    ).snapshot
    return PromptCacheCoordinator.from_legacy_state(
        layout=PromptCacheLayout.APPEND_ONLY,
        cache_epoch_id="initial",
        prefix_message_count=2,
        frozen_tools=[],
        messages=messages,
        cache_epoch_state=epoch,
        append_only_state=_append_only_state(),
    )


def test_sqlite_store_round_trips_append_only_state(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "patchloop.db")
    task = Task(goal="Append", repository=str(tmp_path))
    store.save_task(task)
    coordinator = _append_only_coordinator()
    checkpoint = RuntimeCheckpoint(
        task_id=task.id,
        next_step_index=1,
        messages=[*coordinator.frozen_prefix, ModelMessage(role="user", content="Append")],
        **coordinator.checkpoint_fields(),
    )

    store.save_checkpoint(checkpoint)
    restored = store.get_checkpoint(task.id)

    assert restored.append_only_state == _append_only_state()
    assert restored.cache_epoch_state is not None
    again = PromptCacheCoordinator.from_legacy_state(
        layout=PromptCacheLayout.APPEND_ONLY,
        cache_epoch_id=restored.cache_epoch_state.epoch_id,
        prefix_message_count=restored.prompt_prefix_message_count,
        frozen_tools=restored.tool_specifications or [],
        messages=restored.messages,
        cache_epoch_state=restored.cache_epoch_state,
        memory_publication_state=restored.memory_publication_state,
        append_only_state=restored.append_only_state,
    )
    assert again.append_only_state == coordinator.append_only_state


def test_checkpoint_adapter_keeps_payloads_without_append_only_state(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "patchloop.db")
    task = Task(id="task-1", goal="Resume", repository=str(tmp_path))
    store.save_task(task)
    epoch = CacheEpoch.bootstrap(
        [
            ModelMessage(role="system", content="static"),
            ModelMessage(role="user", content="Resume"),
        ],
        prefix_message_count=2,
        epoch_id="initial",
    )
    payload = RuntimeCheckpoint(
        task_id=task.id,
        next_step_index=0,
        messages=[ModelMessage(role="user", content="Resume")],
        cache_epoch_state=epoch.snapshot,
    ).model_dump(mode="json")
    payload.pop("append_only_state")
    with connect_write(store.path) as connection:
        connection.execute(
            "INSERT INTO checkpoints (task_id, payload_json, updated_at) VALUES (?, ?, ?)",
            (task.id, json.dumps(payload), payload["updated_at"]),
        )

    restored = store.get_checkpoint(task.id)
    assert restored.append_only_state is None
    assert restored.cache_epoch_state is not None


def test_checkpoint_adapter_rejects_corrupt_append_only_state(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "patchloop.db")
    task = Task(id="task-1", goal="Resume", repository=str(tmp_path))
    store.save_task(task)
    coordinator = _append_only_coordinator()
    checkpoint = RuntimeCheckpoint(
        task_id=task.id,
        next_step_index=1,
        messages=coordinator.frozen_prefix,
        **coordinator.checkpoint_fields(),
    )
    payload = checkpoint.model_dump(mode="json")
    payload["append_only_state"]["last_submitted_message_count"] = 1
    with connect_write(store.path) as connection:
        connection.execute(
            "INSERT INTO checkpoints (task_id, payload_json, updated_at) VALUES (?, ?, ?)",
            (task.id, json.dumps(payload), payload["updated_at"]),
        )

    with pytest.raises(CheckpointSchemaError, match="RuntimeCheckpoint schema"):
        store.get_checkpoint(task.id)


def test_decode_task_payload_pins_legacy_layout_for_old_rows() -> None:
    without_execution = json.dumps(
        {"id": "task-1", "goal": "Goal", "repository": "repo", "status": "created"}
    )
    assert (
        _decode_task_payload(without_execution).execution.prompt_cache_layout
        is PromptCacheLayout.LEGACY
    )
    partial_execution = json.dumps(
        {
            "id": "task-2",
            "goal": "Goal",
            "repository": "repo",
            "execution": {"sandbox_backend": "local"},
        }
    )
    assert (
        _decode_task_payload(partial_execution).execution.prompt_cache_layout
        is PromptCacheLayout.LEGACY
    )


def test_decode_task_payload_preserves_explicit_layouts() -> None:
    for index, layout in enumerate(
        (
            PromptCacheLayout.LEGACY,
            PromptCacheLayout.STABLE,
            PromptCacheLayout.APPEND_ONLY,
        )
    ):
        payload = json.dumps(
            {
                "id": f"task-{index}",
                "goal": "Goal",
                "repository": "repo",
                "execution": {"prompt_cache_layout": layout.value},
            }
        )
        assert _decode_task_payload(payload).execution.prompt_cache_layout is layout


def test_decode_task_payload_rejects_structurally_invalid_execution() -> None:
    for execution_value in (None, "legacy", []):
        payload = json.dumps(
            {
                "id": "task-bad",
                "goal": "Goal",
                "repository": "repo",
                "execution": execution_value,
            }
        )
        with pytest.raises(ValidationError):
            _decode_task_payload(payload)


def test_store_task_decode_pins_old_rows_to_legacy(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "patchloop.db")
    legacy_row = json.dumps(
        {"id": "task-old", "goal": "Goal", "repository": "repo", "status": "created"}
    )
    append_only_row = json.dumps(
        {
            "id": "task-new",
            "goal": "Goal",
            "repository": "repo",
            "status": "created",
            "execution": {"prompt_cache_layout": "append_only"},
        }
    )
    with connect_write(store.path) as connection:
        for task_id, row in (("task-old", legacy_row), ("task-new", append_only_row)):
            connection.execute(
                """
                INSERT INTO tasks (
                    id, status, payload_json, updated_at,
                    session_id, outcome, runtime_condition, version
                ) VALUES (?, 'created', ?, ?, NULL, 'active', 'idle', 1)
                """,
                (task_id, row, datetime.now(UTC).isoformat()),
            )

    assert store.get_task("task-old").execution.prompt_cache_layout is PromptCacheLayout.LEGACY
    assert (
        store.get_task("task-new").execution.prompt_cache_layout
        is PromptCacheLayout.APPEND_ONLY
    )


def test_session_and_runtime_checkpoints_project_the_same_append_only_state(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "patchloop.db")
    session = store.create_session(Session(id="session-1", workspace_ref="workspace"))
    task = store.start_task(
        session.id,
        Task(
            id="task-1",
            goal="Append",
            repository="workspace",
            execution=TaskExecutionConfig(prompt_cache_layout=PromptCacheLayout.APPEND_ONLY),
        ),
        expected_version=session.version,
    )
    execution = store.claim_execution(
        Execution(
            id="execution-1",
            session_id=session.id,
            task_id=task.id,
            owner_id="worker-1",
            lease_token="secret-token",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        ),
        expected_version=task.version,
    )
    guard = LeaseGuard(
        execution.id,
        task.id,
        execution.lease_token,
        execution.generation,
        execution.owner_id,
    )
    state = _append_only_state()
    messages = [
        ModelMessage(role="system", content="static"),
        ModelMessage(role="user", content="Append"),
        ModelMessage(role="assistant", content="inspected"),
    ]
    session_checkpoint = SessionCheckpoint(
        session_id=session.id,
        task_id=task.id,
        messages=messages,
        append_only_state=state,
    )
    store.commit_checkpoint(
        session_checkpoint,
        expected_version=store.get_task(task.id).version,
        lease_guard=guard,
    )

    restored_session = store.get_session_checkpoint(task.id)
    assert restored_session.append_only_state == state

    runtime_checkpoint = RuntimeCheckpoint(
        task_id=task.id,
        session_id=session.id,
        next_step_index=1,
        messages=messages,
        append_only_state=restored_session.append_only_state,
    )
    assert runtime_checkpoint.append_only_state == restored_session.append_only_state
    assert RuntimeCheckpoint.model_validate_json(
        runtime_checkpoint.model_dump_json()
    ).append_only_state == state
