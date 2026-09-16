import json
from pathlib import Path

import pytest

from patchloop.domain import (
    AppendOnlyOptimizationVersion,
    PromptCacheLayout,
    Task,
    TaskExecutionConfig,
    TaskRuntimeCondition,
    TaskStatus,
    ToolCall,
)
from patchloop.execution.approvals import ApprovalService
from patchloop.execution.models import ApprovalStatus, EffectStatus
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import ApprovalConflict
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.runtime import AgentRuntime
from patchloop.security import PolicyDecision, RiskLevel
from patchloop.tools import (
    PermissionLevel,
    ReplaceTextTool,
    ToolContext,
    ToolGateway,
    ToolPolicy,
    UpdatePlanTool,
)


def _plan_response() -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            ToolCall(
                id="plan-1",
                name="update_plan",
                arguments={
                    "items": [
                        {"description": "Update README", "status": "running"},
                        {"description": "Verify README", "status": "pending"},
                    ]
                },
            )
        ]
    )


def _replace_response(*, path: str = "README.md") -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            ToolCall(
                id="replace-1",
                name="replace_text",
                arguments={
                    "path": path,
                    "old_text": "Before",
                    "new_text": "After",
                },
            )
        ]
    )


@pytest.mark.parametrize("layout", [PromptCacheLayout.LEGACY, PromptCacheLayout.APPEND_ONLY])
def test_runtime_persists_approval_and_yields_without_tool_failure(
    tmp_path: Path, layout: PromptCacheLayout
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    target = repository / "README.md"
    target.write_text("# Before\n", encoding="utf-8")
    store = SQLiteStore(tmp_path / "state.db")
    callbacks: list[str] = []
    policy = ToolPolicy(
        frozenset({PermissionLevel.READ, PermissionLevel.WRITE}),
        approval_threshold=RiskLevel.MEDIUM,
        approval_handler=lambda request: callbacks.append(request.call_id) or True,
    )
    provider = FakeProvider([_plan_response(), _replace_response()])
    task = Task(
        id="task-1",
        goal="Update README",
        repository=str(repository),
        execution=TaskExecutionConfig(prompt_cache_layout=layout),
    )

    result = AgentRuntime(
        provider,
        ToolGateway(
            ToolContext(repository),
            [UpdatePlanTool(), ReplaceTextTool()],
            policy=policy,
        ),
        state_store=store,
        owner_id="approval-worker",
    ).run(task)

    assert result.status is TaskStatus.RUNNING
    assert result.runtime_condition is TaskRuntimeCondition.WAITING_FOR_APPROVAL
    assert result.error is None
    assert target.read_text(encoding="utf-8") == "# Before\n"
    assert callbacks == []
    effects = store.list_effects(task.id)
    waiting = next(effect for effect in effects if effect.provider_call_id == "replace-1")
    assert waiting.status is EffectStatus.WAITING_FOR_APPROVAL
    assert waiting.policy_result["decision"] == PolicyDecision.REQUIRE_APPROVAL.value
    assert store.get_tool_result(task.id, "replace-1") is None
    approval = store.get_approval(waiting.approval_id or "")
    assert approval.status is ApprovalStatus.PENDING
    assert approval.effect_id == waiting.id
    assert approval.policy_version == policy.version
    assert approval.config_version == "1"
    assert store.list_approvals(task.id) == [approval]

    checkpoint = store.get_checkpoint(task.id)
    resume_provider = FakeProvider([])
    resumed = AgentRuntime(
        resume_provider,
        ToolGateway(
            ToolContext(repository),
            [UpdatePlanTool(), ReplaceTextTool()],
            policy=policy,
        ),
        state_store=store,
        owner_id="approval-worker-after-restart",
    ).resume(store.get_task(task.id), checkpoint)

    assert resumed.runtime_condition is TaskRuntimeCondition.WAITING_FOR_APPROVAL
    assert resume_provider.requests == []
    assert store.list_approvals(task.id) == [approval]
    assert target.read_text(encoding="utf-8") == "# Before\n"

    decided, authorized_effect, ready_task = ApprovalService(store).decide(
        approval.id,
        approved=True,
        source="operator",
        expected_version=approval.version,
        workspace_ref=str(repository),
        policy_version=policy.version,
        config_version="1",
    )
    assert decided.status is ApprovalStatus.APPROVED
    assert authorized_effect.status is EffectStatus.PREPARED
    assert ready_task.runtime_condition is TaskRuntimeCondition.IDLE
    assert target.read_text(encoding="utf-8") == "# Before\n"
    assert (
        ApprovalService(store).decide(
            approval.id,
            approved=True,
            source="operator",
            expected_version=approval.version,
            workspace_ref=str(repository),
            policy_version=policy.version,
            config_version="1",
        )[0]
        == decided
    )
    with pytest.raises(ApprovalConflict):
        ApprovalService(store).decide(
            approval.id,
            approved=False,
            source="operator",
            expected_version=decided.version,
            workspace_ref=str(repository),
            policy_version=policy.version,
            config_version="1",
        )

    completion_provider = FakeProvider([ModelResponse(content="README updated.")])
    completed = AgentRuntime(
        completion_provider,
        ToolGateway(
            ToolContext(repository),
            [UpdatePlanTool(), ReplaceTextTool()],
            policy=policy,
        ),
        state_store=store,
        owner_id="approved-effect-worker",
    ).resume(store.get_task(task.id), checkpoint)

    assert completed.status is TaskStatus.COMPLETED
    assert target.read_text(encoding="utf-8") == "# After\n"
    committed_effect = store.get_effect(authorized_effect.id)
    assert committed_effect.status is EffectStatus.SUCCEEDED
    assert committed_effect.approval_id is None
    assert committed_effect.approval_consumed is True
    assert store.get_approval(approval.id).status is ApprovalStatus.CONSUMED
    assert store.get_tool_result(task.id, "replace-1") is not None
    assert len(completion_provider.requests) == 1
    assert (
        ApprovalService(store)
        .decide(
            approval.id,
            approved=True,
            source="operator",
            expected_version=approval.version,
            workspace_ref=str(repository),
            policy_version=policy.version,
            config_version="1",
        )[0]
        .status
        is ApprovalStatus.CONSUMED
    )


def test_hard_permission_denial_cannot_be_approved(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    target = repository / "README.md"
    target.write_text("# Before\n", encoding="utf-8")
    store = SQLiteStore(tmp_path / "state.db")
    callbacks: list[str] = []
    policy = ToolPolicy(
        frozenset({PermissionLevel.READ}),
        approval_threshold=RiskLevel.MEDIUM,
        approval_handler=lambda request: callbacks.append(request.call_id) or True,
    )
    provider = FakeProvider(
        [_plan_response(), _replace_response(), ModelResponse(content="Write was denied.")]
    )
    task = Task(id="task-1", goal="Update README", repository=str(repository))

    result = AgentRuntime(
        provider,
        ToolGateway(
            ToolContext(repository),
            [UpdatePlanTool(), ReplaceTextTool()],
            policy=policy,
        ),
        state_store=store,
        owner_id="denied-worker",
    ).run(task)

    assert result.status is TaskStatus.COMPLETED
    assert target.read_text(encoding="utf-8") == "# Before\n"
    assert callbacks == []
    denied = next(
        effect for effect in store.list_effects(task.id) if effect.provider_call_id == "replace-1"
    )
    assert denied.policy_result["decision"] == PolicyDecision.DENY.value
    assert denied.preparation_error is not None
    assert denied.status is EffectStatus.DENIED
    observation = store.get_tool_result(task.id, "replace-1")
    assert observation is not None
    payload = json.loads(observation.output)
    assert payload["backend_invoked"] is False
    assert payload["effect_status"] == "denied"
    assert payload["next_action"] == "choose_alternative"
    assert store.list_approvals(task.id) == []


@pytest.mark.parametrize(
    ("layout", "optimization_version"),
    [
        (PromptCacheLayout.LEGACY, "baseline_v1"),
        (PromptCacheLayout.APPEND_ONLY, "baseline_v1"),
        (PromptCacheLayout.APPEND_ONLY, "balanced_v1"),
    ],
)
def test_operator_can_deny_pending_effect(
    tmp_path: Path,
    layout: PromptCacheLayout,
    optimization_version: AppendOnlyOptimizationVersion,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    target = repository / "README.md"
    target.write_text("# Before\n", encoding="utf-8")
    store = SQLiteStore(tmp_path / "state.db")
    policy = ToolPolicy(
        frozenset({PermissionLevel.READ, PermissionLevel.WRITE}),
        approval_threshold=RiskLevel.MEDIUM,
    )
    task = Task(
        id="task-1",
        goal="Update README",
        repository=str(repository),
        execution=TaskExecutionConfig(
            prompt_cache_layout=layout,
            append_only_optimization=optimization_version,
        ),
    )
    AgentRuntime(
        FakeProvider([_plan_response(), _replace_response()]),
        ToolGateway(
            ToolContext(repository),
            [UpdatePlanTool(), ReplaceTextTool()],
            policy=policy,
        ),
        state_store=store,
    ).run(task)
    approval = store.list_approvals(task.id)[0]

    decided, denied_effect, ready_task = ApprovalService(store).decide(
        approval.id,
        approved=False,
        source="operator",
        expected_version=approval.version,
        workspace_ref=str(repository),
        policy_version=policy.version,
        config_version="1",
    )

    assert decided.status is ApprovalStatus.DENIED
    assert denied_effect.status is EffectStatus.DENIED
    assert ready_task.runtime_condition is TaskRuntimeCondition.IDLE
    assert target.read_text(encoding="utf-8") == "# Before\n"

    resume_provider = FakeProvider([ModelResponse(content="Selected a safe alternative.")])
    result = AgentRuntime(
        resume_provider,
        ToolGateway(
            ToolContext(repository),
            [UpdatePlanTool(), ReplaceTextTool()],
            policy=policy,
        ),
        state_store=store,
        owner_id="denial-observer",
    ).resume(store.get_task(task.id), store.get_checkpoint(task.id))

    assert result.status is TaskStatus.COMPLETED
    assert len(resume_provider.requests) == 1
    observation = store.get_tool_result(task.id, "replace-1")
    assert observation is not None
    payload = json.loads(observation.output)
    assert payload["backend_invoked"] is False
    assert payload["effect_status"] == "denied"
    assert payload["next_action"] == "choose_alternative"
    assert target.read_text(encoding="utf-8") == "# Before\n"


def test_changed_execution_conditions_expire_old_approval(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "README.md").write_text("# Before\n", encoding="utf-8")
    store = SQLiteStore(tmp_path / "state.db")
    policy = ToolPolicy(
        frozenset({PermissionLevel.READ, PermissionLevel.WRITE}),
        approval_threshold=RiskLevel.MEDIUM,
    )
    task = Task(id="task-1", goal="Update README", repository=str(repository))
    AgentRuntime(
        FakeProvider([_plan_response(), _replace_response()]),
        ToolGateway(
            ToolContext(repository),
            [UpdatePlanTool(), ReplaceTextTool()],
            policy=policy,
        ),
        state_store=store,
    ).run(task)
    approval = store.list_approvals(task.id)[0]

    expired, waiting_effect, waiting_task = ApprovalService(store).decide(
        approval.id,
        approved=True,
        source="operator",
        expected_version=approval.version,
        workspace_ref=str(repository),
        policy_version="changed-policy",
        config_version="1",
    )

    assert expired.status is ApprovalStatus.EXPIRED
    assert waiting_effect.status is EffectStatus.PREPARED
    assert waiting_effect.approval_id is None
    assert waiting_task.runtime_condition is TaskRuntimeCondition.IDLE
