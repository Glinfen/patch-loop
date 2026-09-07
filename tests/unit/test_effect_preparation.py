import hashlib
from pathlib import Path

import pytest

from patchloop.domain import AgentStep, Plan, PlanItem, StepStatus, Task, ToolCall
from patchloop.execution.effects import (
    assert_file_preconditions,
    persist_model_response_batch,
    restore_file_preconditions,
    revalidate_effect_call,
)
from patchloop.persistence import SQLiteStore
from patchloop.providers import ModelResponse
from patchloop.security import PolicyDecision, RiskLevel
from patchloop.tools import (
    CreateFileTool,
    PermissionLevel,
    ReplaceTextTool,
    ToolContext,
    ToolGateway,
    ToolPolicy,
)


def _step(task: Task) -> AgentStep:
    return AgentStep(task_id=task.id, index=0, status=StepStatus.RUNNING)


def _write_gateway(repository: Path, *, policy: ToolPolicy | None = None) -> ToolGateway:
    context = ToolContext(repository)
    context.plan = Plan(items=[PlanItem(description="Update the file", status=StepStatus.RUNNING)])
    return ToolGateway(
        context,
        [CreateFileTool(), ReplaceTextTool()],
        policy=policy
        or ToolPolicy(frozenset({PermissionLevel.WRITE}), require_plan_for_mutations=True),
    )


def test_prepare_normalizes_arguments_and_captures_file_baseline(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    readme = repository / "README.md"
    original = "# Before\n"
    readme.write_text(original, encoding="utf-8")
    task = Task(id="task-1", goal="Update README", repository=str(repository))
    gateway = _write_gateway(repository)
    response = ModelResponse(
        tool_calls=[
            ToolCall(
                id="replace-1",
                name="replace_text",
                arguments={
                    "path": "README.md",
                    "old_text": "Before",
                    "new_text": "After",
                },
            )
        ]
    )

    _, effects = persist_model_response_batch(task, _step(task), response, gateway)

    effect = effects[0]
    assert effect.action_kind == "write"
    assert effect.arguments_summary["expected_occurrences"] == 1
    assert effect.preparation_error is None
    assert effect.policy_result["allowed"] is True
    assert effect.policy_result["decision"] == PolicyDecision.ALLOW.value
    assert len(effect.file_preconditions) == 1
    precondition = effect.file_preconditions[0]
    assert precondition.path == "README.md"
    assert precondition.existed is True
    assert precondition.original_content == original
    assert precondition.original_sha256 == hashlib.sha256(original.encode()).hexdigest()
    assert precondition.target_sha256 == hashlib.sha256(b"# After\n").hexdigest()
    assert gateway.context.changes.snapshot() == {"README.md": original}
    assert readme.read_text(encoding="utf-8") == original


def test_prepare_records_missing_file_marker_and_restores_diff_baseline(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    task = Task(id="task-1", goal="Create notes", repository=str(repository))
    gateway = _write_gateway(repository)
    response = ModelResponse(
        tool_calls=[
            ToolCall(
                id="create-1",
                name="create_file",
                arguments={"path": "notes.txt", "content": "created\n"},
            )
        ]
    )

    _, effects = persist_model_response_batch(task, _step(task), response, gateway)
    precondition = effects[0].file_preconditions[0]
    assert precondition.existed is False
    assert precondition.original_content is None
    assert precondition.original_sha256 is None

    (repository / "notes.txt").write_text("created\n", encoding="utf-8")
    recovered_gateway = _write_gateway(repository)
    restore_file_preconditions(recovered_gateway, effects)
    assert "+++ b/notes.txt" in recovered_gateway.context.changes.diff()


def test_prepare_projects_prior_mutations_within_one_batch(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    task = Task(id="task-1", goal="Create and update notes", repository=str(repository))
    gateway = _write_gateway(repository)
    response = ModelResponse(
        tool_calls=[
            ToolCall(
                id="create-1",
                name="create_file",
                arguments={"path": "notes.txt", "content": "draft\n"},
            ),
            ToolCall(
                id="replace-1",
                name="replace_text",
                arguments={
                    "path": "notes.txt",
                    "old_text": "draft",
                    "new_text": "final",
                },
            ),
        ]
    )

    _, effects = persist_model_response_batch(task, _step(task), response, gateway)

    assert [effect.preparation_error for effect in effects] == [None, None]
    assert effects[0].file_preconditions[0].existed is False
    assert effects[1].file_preconditions[0].original_content == "draft\n"
    assert effects[1].file_preconditions[0].target_sha256 == hashlib.sha256(b"final\n").hexdigest()
    assert gateway.context.changes.snapshot() == {"notes.txt": None}


def test_prepare_captures_validation_and_plan_failures_without_writing(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    target = repository / "README.md"
    target.write_text("# Before\n", encoding="utf-8")
    task = Task(id="task-1", goal="Update README", repository=str(repository))
    gateway = ToolGateway(
        ToolContext(repository),
        [ReplaceTextTool()],
        policy=ToolPolicy(frozenset({PermissionLevel.WRITE})),
    )
    response = ModelResponse(
        tool_calls=[
            ToolCall(
                name="replace_text",
                arguments={
                    "path": "README.md",
                    "old_text": "missing",
                    "new_text": "After",
                },
            )
        ]
    )

    _, effects = persist_model_response_batch(task, _step(task), response, gateway)

    assert effects[0].preparation_error == "an execution plan is required before using replace_text"
    assert effects[0].policy_result["allowed"] is False
    assert effects[0].file_preconditions == []
    assert target.read_text(encoding="utf-8") == "# Before\n"


def test_prepare_policy_assessment_does_not_invoke_approval_handler(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "README.md").write_text("# Before\n", encoding="utf-8")
    approvals: list[str] = []
    policy = ToolPolicy(
        frozenset({PermissionLevel.WRITE}),
        require_plan_for_mutations=False,
        approval_threshold=RiskLevel.MEDIUM,
        approval_handler=lambda request: approvals.append(request.call_id) or True,
    )
    gateway = _write_gateway(repository, policy=policy)
    task = Task(id="task-1", goal="Update README", repository=str(repository))
    response = ModelResponse(
        tool_calls=[
            ToolCall(
                id="replace-1",
                name="replace_text",
                arguments={
                    "path": "README.md",
                    "old_text": "Before",
                    "new_text": "After",
                },
            )
        ]
    )

    _, effects = persist_model_response_batch(task, _step(task), response, gateway)

    assert approvals == []
    assert effects[0].policy_result["approval_required"] is True
    assert effects[0].policy_result["allowed"] is False
    assert effects[0].preparation_error is None


def test_sqlite_round_trip_preserves_preparation_and_recovery_data(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    original = "# Before\n"
    (repository / "README.md").write_text(original, encoding="utf-8")
    task = Task(id="task-1", goal="Update README", repository=str(repository))
    gateway = _write_gateway(repository)
    step, effects = persist_model_response_batch(
        task,
        _step(task),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="replace-1",
                    name="replace_text",
                    arguments={
                        "path": "README.md",
                        "old_text": "Before",
                        "new_text": "After",
                    },
                )
            ]
        ),
        gateway,
    )
    store = SQLiteStore(tmp_path / "state.db")
    store.save_task(task)

    store.prepare_effect_batch(step, effects, expected_version=task.version)

    persisted = store.get_effect(effects[0].id)
    assert persisted.arguments_summary["expected_occurrences"] == 1
    assert persisted.policy_result == effects[0].policy_result
    assert persisted.preparation_error is None
    assert persisted.file_preconditions[0].original_content == original
    assert (
        persisted.file_preconditions[0].target_sha256
        == effects[0].file_preconditions[0].target_sha256
    )


def test_execution_rechecks_normalized_input_and_file_baseline(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    target = repository / "README.md"
    target.write_text("# Before\n", encoding="utf-8")
    task = Task(id="task-1", goal="Update README", repository=str(repository))
    gateway = _write_gateway(repository)
    call = ToolCall(
        id="replace-1",
        name="replace_text",
        arguments={
            "path": "README.md",
            "old_text": "Before",
            "new_text": "After",
        },
    )
    _, effects = persist_model_response_batch(
        task,
        _step(task),
        ModelResponse(tool_calls=[call]),
        gateway,
    )
    effect = effects[0]

    executable = revalidate_effect_call(effect, call, gateway)

    assert executable.arguments["expected_occurrences"] == 1
    with pytest.raises(ValueError, match="arguments changed"):
        revalidate_effect_call(
            effect,
            call.model_copy(
                update={
                    "arguments": {
                        **call.arguments,
                        "new_text": "Different",
                    }
                }
            ),
            gateway,
        )

    target.write_text("# User edit\n", encoding="utf-8")
    with pytest.raises(ValueError, match="file precondition changed"):
        assert_file_preconditions(effect, gateway)
