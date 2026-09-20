import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from patchloop.domain import (
    AgentStep,
    AppendOnlyOptimizationVersion,
    ErrorKind,
    PromptCacheLayout,
    StepStatus,
    Task,
    TaskExecutionConfig,
    TaskRuntimeCondition,
    TaskStatus,
    ToolCall,
    ToolResult,
)
from patchloop.execution.approvals import ApprovalService, build_approval
from patchloop.execution.effects import (
    arguments_fingerprint,
    persist_model_response_batch,
    stable_effect_id,
)
from patchloop.execution.models import ControlKind, ControlRequest, EffectStatus
from patchloop.execution.recovery import RecoveryService
from patchloop.persistence import SQLiteStore
from patchloop.providers import FakeProvider, ModelResponse, ModelUsage
from patchloop.runtime import AgentRuntime
from patchloop.security import RiskLevel
from patchloop.tools import (
    ListFilesTool,
    PermissionLevel,
    ReplaceTextTool,
    Tool,
    ToolContext,
    ToolGateway,
    ToolPolicy,
    UpdatePlanTool,
)
from patchloop.tools.base import ToolInputModel


class _InterruptBeforeFirstToolGateway(ToolGateway):
    def execute(self, task_id: str, call: ToolCall) -> ToolResult:
        raise KeyboardInterrupt("crash after Effect preparation")


class _InterruptAfterReplaceGateway(ToolGateway):
    def execute_claimed(
        self,
        task_id: str,
        call: ToolCall,
        *,
        approval_consumed: bool,
    ) -> ToolResult:
        result = super().execute_claimed(
            task_id,
            call,
            approval_consumed=approval_consumed,
        )
        if call.name == "replace_text":
            raise KeyboardInterrupt("crash after file mutation")
        return result


class _CountingListGateway(ToolGateway):
    def __init__(self, repository: Path, policy: ToolPolicy, calls: list[str]) -> None:
        super().__init__(ToolContext(repository), [ListFilesTool()], policy=policy)
        self.calls = calls

    def execute(self, task_id: str, call: ToolCall) -> ToolResult:
        self.calls.append(call.id)
        return super().execute(task_id, call)


class _ConfirmedFailureInput(ToolInputModel):
    pass


class _ConfirmedFailureTool(Tool):
    name = "confirmed_failure"
    description = "Return a backend-confirmed failure."
    input_model = _ConfirmedFailureInput
    permission = PermissionLevel.WRITE

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        self.calls.append("invoked")
        return "backend confirmed the action failed"

    def classify_output(self, output: str) -> ErrorKind | None:
        return ErrorKind.EXECUTION_ERROR


class _InterruptAfterFailureCommitStore(SQLiteStore):
    interrupted = False

    def commit_effect(self, effect, **kwargs):
        committed = super().commit_effect(effect, **kwargs)
        if effect.provider_call_id == "failure-1" and not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt("crash after confirmed failure commit")
        return committed


class _CancelAfterPrepareStore(SQLiteStore):
    requested = False

    def prepare_effect_batch(self, step, effects, **kwargs):
        prepared = super().prepare_effect_batch(step, effects, **kwargs)
        lease_guard = kwargs.get("lease_guard")
        if not self.requested and prepared and lease_guard is not None:
            self.requested = True
            task = self.get_task(step.task_id)
            self.request_control(
                ControlRequest(
                    task_id=task.id,
                    execution_id=lease_guard.execution_id,
                    kind=ControlKind.CANCEL,
                ),
                expected_version=task.version,
            )
        return prepared


class _ModifyFileAfterPrepareStore(SQLiteStore):
    def __init__(self, path: Path, target: Path) -> None:
        super().__init__(path)
        self.target = target
        self.modified = False

    def prepare_effect_batch(self, step, effects, **kwargs):
        prepared = super().prepare_effect_batch(step, effects, **kwargs)
        if not self.modified and any(effect.tool_name == "replace_text" for effect in prepared):
            self.modified = True
            self.target.write_text("# Updated requirement\n", encoding="utf-8")
        return prepared


def _gateway(repository: Path, *, interrupt: bool = False) -> ToolGateway:
    gateway_type = _InterruptBeforeFirstToolGateway if interrupt else ToolGateway
    return gateway_type(ToolContext(repository), [ListFilesTool()])


def test_effect_identity_is_stable_and_preserves_model_batch_order() -> None:
    task = Task(id="task-1", goal="Inspect", repository="workspace")
    step = AgentStep(id="step-1", task_id=task.id, index=2, status=StepStatus.RUNNING)
    response = ModelResponse(
        content="Inspect both views",
        tool_calls=[
            ToolCall(id="provider-b", name="list_files", arguments={"path": "b"}),
            ToolCall(id="provider-a", name="list_files", arguments={"path": "a"}),
        ],
        usage=ModelUsage(input_tokens=7, output_tokens=3, cost_usd=0.02),
    )

    persisted_step, effects = persist_model_response_batch(
        task,
        step,
        response,
        _gateway(Path.cwd()),
    )

    assert persisted_step.model_response == response.model_dump(mode="json")
    assert [effect.provider_call_id for effect in effects] == ["provider-b", "provider-a"]
    assert [effect.batch_position for effect in effects] == [0, 1]
    assert persisted_step.effect_ids == [effect.id for effect in effects]
    assert effects[0].id == stable_effect_id(task.id, step.id, 0)
    assert effects[0].id != effects[1].id


def test_resume_marks_unverifiable_executing_effect_unknown(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    store = SQLiteStore(tmp_path / "state.db")
    original_response = ModelResponse(
        content="Inspect the repository",
        tool_calls=[ToolCall(id="provider-call-1", name="list_files")],
        usage=ModelUsage(input_tokens=7, output_tokens=3, cost_usd=0.02),
    )
    first_provider = FakeProvider([original_response])
    task = Task(id="task-1", goal="Inspect", repository=str(repository))

    with pytest.raises(KeyboardInterrupt, match="Effect preparation"):
        AgentRuntime(
            first_provider,
            _gateway(repository, interrupt=True),
            state_store=store,
            owner_id="worker-before-crash",
        ).run(task)

    persisted_step = store.list_steps(task.id)[0]
    effects = store.list_effects(task.id)
    assert persisted_step.model_response == original_response.model_dump(mode="json")
    assert persisted_step.effect_ids == [effects[0].id]
    assert effects[0].provider_call_id == "provider-call-1"
    assert effects[0].batch_position == 0
    checkpoint = store.get_checkpoint(task.id)

    resume_provider = FakeProvider([])
    result = AgentRuntime(
        resume_provider,
        _gateway(repository),
        state_store=store,
        owner_id="worker-after-crash",
    ).resume(store.get_task(task.id), checkpoint)

    assert result.status is TaskStatus.RUNNING
    assert result.runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED
    assert len(first_provider.requests) == 1
    assert resume_provider.requests == []
    effect = store.list_effects(task.id)[0]
    assert effect.status is EffectStatus.UNKNOWN
    assert effect.reconciliation_evidence["source"] == "result_missing"
    assert store.list_tool_results(task.id) == []


def _file_gateway(repository: Path, *, interrupt: bool = False) -> ToolGateway:
    gateway_type = _InterruptAfterReplaceGateway if interrupt else ToolGateway
    return gateway_type(
        ToolContext(repository),
        [UpdatePlanTool(), ReplaceTextTool()],
        policy=ToolPolicy(
            frozenset({PermissionLevel.READ, PermissionLevel.WRITE}),
            approval_threshold=None,
        ),
    )


def _file_responses() -> list[ModelResponse]:
    return [
        ModelResponse(
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
        ),
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
    ]


@pytest.mark.parametrize("optimization_version", ["baseline_v1", "balanced_v1"])
def test_resume_confirms_completed_file_effect_from_target_digest(
    tmp_path: Path,
    optimization_version: AppendOnlyOptimizationVersion,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    target = repository / "README.md"
    target.write_text("# Before\n", encoding="utf-8")
    store = SQLiteStore(tmp_path / "state.db")
    task = Task(
        id="task-1",
        goal="Update README",
        repository=str(repository),
        execution=TaskExecutionConfig(
            prompt_cache_layout=PromptCacheLayout.APPEND_ONLY,
            append_only_optimization=optimization_version,
        ),
    )

    with pytest.raises(KeyboardInterrupt, match="file mutation"):
        AgentRuntime(
            FakeProvider(_file_responses()),
            _file_gateway(repository, interrupt=True),
            state_store=store,
            owner_id="worker-before-crash",
        ).run(task)

    assert target.read_text(encoding="utf-8") == "# After\n"
    interrupted = next(
        effect for effect in store.list_effects(task.id) if effect.provider_call_id == "replace-1"
    )
    assert interrupted.status is EffectStatus.EXECUTING
    assert store.get_tool_result(task.id, "replace-1") is None

    resume_provider = FakeProvider([ModelResponse(content="Recovered safely.")])
    result = AgentRuntime(
        resume_provider,
        _file_gateway(repository),
        state_store=store,
        owner_id="worker-after-crash",
    ).resume(store.get_task(task.id), store.get_checkpoint(task.id))

    assert result.status is TaskStatus.COMPLETED
    assert len(resume_provider.requests) == 1
    reconciled = store.get_effect(interrupted.id)
    assert reconciled.status is EffectStatus.SUCCEEDED
    assert reconciled.reconciliation_evidence["source"] == "file_target_digest"
    assert store.get_tool_result(task.id, "replace-1") is not None
    assert target.read_text(encoding="utf-8") == "# After\n"


def test_resume_requires_recovery_when_file_state_cannot_be_proven(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    target = repository / "README.md"
    target.write_text("# Before\n", encoding="utf-8")
    store = SQLiteStore(tmp_path / "state.db")
    task = Task(id="task-1", goal="Update README", repository=str(repository))

    with pytest.raises(KeyboardInterrupt, match="file mutation"):
        AgentRuntime(
            FakeProvider(_file_responses()),
            _file_gateway(repository, interrupt=True),
            state_store=store,
            owner_id="worker-before-crash",
        ).run(task)
    target.write_text("# User edit\n", encoding="utf-8")

    resume_provider = FakeProvider([])
    result = AgentRuntime(
        resume_provider,
        _file_gateway(repository),
        state_store=store,
        owner_id="worker-after-crash",
    ).resume(store.get_task(task.id), store.get_checkpoint(task.id))

    assert result.runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED
    assert resume_provider.requests == []
    interrupted = next(
        effect for effect in store.list_effects(task.id) if effect.provider_call_id == "replace-1"
    )
    assert interrupted.status is EffectStatus.UNKNOWN
    assert interrupted.reconciliation_evidence["source"] == "file_state_unconfirmed"
    assert store.get_tool_result(task.id, "replace-1") is None
    assert target.read_text(encoding="utf-8") == "# User edit\n"


def test_confirmed_failure_is_replayed_without_becoming_unknown(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = _InterruptAfterFailureCommitStore(tmp_path / "state.db")
    calls: list[str] = []
    task = Task(id="task-1", goal="Try an action", repository=str(repository))
    policy = ToolPolicy(
        frozenset({PermissionLevel.WRITE}),
        require_plan_for_mutations=False,
    )

    with pytest.raises(KeyboardInterrupt, match="confirmed failure commit"):
        AgentRuntime(
            FakeProvider(
                [ModelResponse(tool_calls=[ToolCall(id="failure-1", name="confirmed_failure")])]
            ),
            ToolGateway(
                ToolContext(repository),
                [_ConfirmedFailureTool(calls)],
                policy=policy,
            ),
            state_store=store,
            owner_id="worker-before-crash",
        ).run(task)

    failed = next(
        effect for effect in store.list_effects(task.id) if effect.provider_call_id == "failure-1"
    )
    assert failed.status is EffectStatus.FAILED
    assert store.get_tool_result(task.id, "failure-1") is not None
    assert calls == ["invoked"]

    resumed_gateway = ToolGateway(
        ToolContext(repository),
        [_ConfirmedFailureTool(calls)],
        policy=policy,
    )
    result = AgentRuntime(
        FakeProvider([ModelResponse(content="Chose a safe alternative.")]),
        resumed_gateway,
        state_store=store,
        owner_id="worker-after-crash",
    ).resume(store.get_task(task.id), store.get_checkpoint(task.id))

    assert result.status is TaskStatus.COMPLETED
    assert store.get_effect(failed.id).status is EffectStatus.FAILED
    assert resumed_gateway.context.requires_replan is True
    assert calls == ["invoked"]


def test_cancelled_prepared_effect_is_paired_without_backend_execution(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = _CancelAfterPrepareStore(tmp_path / "state.db")
    calls: list[str] = []
    task = Task(id="task-1", goal="Action superseded by input", repository=str(repository))
    policy = ToolPolicy(
        frozenset({PermissionLevel.WRITE}),
        require_plan_for_mutations=False,
    )

    result = AgentRuntime(
        FakeProvider(
            [ModelResponse(tool_calls=[ToolCall(id="cancel-1", name="confirmed_failure")])]
        ),
        ToolGateway(
            ToolContext(repository),
            [_ConfirmedFailureTool(calls)],
            policy=policy,
        ),
        state_store=store,
        owner_id="cancelling-worker",
    ).run(task)

    assert result.status is TaskStatus.CANCELLED
    cancelled = next(
        effect for effect in store.list_effects(task.id) if effect.provider_call_id == "cancel-1"
    )
    assert cancelled.status is EffectStatus.CANCELLED
    observation = store.get_tool_result(task.id, "cancel-1")
    assert observation is not None
    assert json.loads(observation.output) == {
        "backend_invoked": False,
        "effect_status": "cancelled",
        "next_action": "await_updated_requirements",
        "reason": "task cancelled before backend execution",
    }
    assert calls == []


def test_changed_file_cancels_prepared_effect_with_paired_observation(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    target = repository / "README.md"
    target.write_text("# Before\n", encoding="utf-8")
    store = _ModifyFileAfterPrepareStore(tmp_path / "state.db", target)
    task = Task(id="task-1", goal="Update README", repository=str(repository))
    provider = FakeProvider([*_file_responses(), ModelResponse(content="Requirements changed.")])

    result = AgentRuntime(
        provider,
        _file_gateway(repository),
        state_store=store,
        owner_id="constraint-aware-worker",
    ).run(task)

    assert result.status is TaskStatus.COMPLETED
    cancelled = next(
        effect for effect in store.list_effects(task.id) if effect.provider_call_id == "replace-1"
    )
    assert cancelled.status is EffectStatus.CANCELLED
    observation = store.get_tool_result(task.id, "replace-1")
    assert observation is not None
    assert json.loads(observation.output) == {
        "backend_invoked": False,
        "effect_status": "cancelled",
        "next_action": "await_updated_requirements",
        "reason": "file precondition changed: README.md",
    }
    assert target.read_text(encoding="utf-8") == "# Updated requirement\n"


def test_explicit_approved_retry_executes_new_effect_once(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    store = SQLiteStore(tmp_path / "state.db")
    task = Task(id="task-1", goal="Inspect safely", repository=str(repository))

    with pytest.raises(KeyboardInterrupt, match="Effect preparation"):
        AgentRuntime(
            FakeProvider(
                [ModelResponse(tool_calls=[ToolCall(id="original-call", name="list_files")])]
            ),
            _gateway(repository, interrupt=True),
            state_store=store,
            owner_id="worker-before-crash",
        ).run(task)

    AgentRuntime(
        FakeProvider([]),
        _gateway(repository),
        state_store=store,
        owner_id="reconciliation-worker",
    ).resume(store.get_task(task.id), store.get_checkpoint(task.id))
    recovery_task = store.get_task(task.id)
    unknown = next(
        effect
        for effect in store.list_effects(task.id)
        if effect.provider_call_id == "original-call"
    )
    assert unknown.status is EffectStatus.UNKNOWN

    retry_policy = ToolPolicy(
        frozenset({PermissionLevel.READ}),
        approval_threshold=RiskLevel.LOW,
    )
    retry_call = ToolCall(
        id="retry-call",
        name=unknown.tool_name,
        arguments=unknown.arguments_summary,
    )
    retry_context = ToolContext(repository)
    retry_context.session_id = recovery_task.session_id or ""
    retry_preparation = ToolGateway(
        retry_context,
        [ListFilesTool()],
        policy=retry_policy,
    ).prepare_call(task.id, retry_call)
    retry = unknown.model_copy(
        update={
            "id": "effect-retry",
            "step_id": "step-retry",
            "provider_call_id": retry_call.id,
            "retry_of_effect_id": unknown.id,
            "status": EffectStatus.PREPARED,
            "approval_id": None,
            "approval_consumed": False,
            "arguments_summary": retry_preparation.normalized_arguments,
            "arguments_fingerprint": arguments_fingerprint(retry_preparation.normalized_arguments),
            "policy_result": retry_preparation.policy_result.model_dump(mode="json"),
            "policy_evaluation": retry_preparation.policy_evaluation,
            "action_descriptor": retry_preparation.action_descriptor,
            "reconciliation_evidence": {},
            "result_ref": None,
            "observation_ref": None,
            "version": 1,
        }
    )
    approval = build_approval(
        recovery_task,
        retry,
        policy_version=retry_policy.version,
        config_version="1",
    )
    resolution = RecoveryService(store).create_retry(
        task_id=task.id,
        unknown_effect_id=unknown.id,
        retry_effect=retry,
        approval=approval,
        duplicate_risk_acknowledged=True,
        evidence={"duplicate_risk_acknowledged": True},
        decision_source="operator",
        expected_version=recovery_task.version,
    )
    _, authorized, ready_task = ApprovalService(store).decide(
        approval.id,
        approved=True,
        source="operator",
        expected_version=approval.version,
        workspace_ref=str(repository),
        policy_version=retry_policy.version,
        config_version="1",
    )
    assert resolution.task.runtime_condition is TaskRuntimeCondition.WAITING_FOR_APPROVAL
    assert ready_task.runtime_condition is TaskRuntimeCondition.IDLE
    assert authorized.status is EffectStatus.PREPARED

    backend_calls: list[str] = []
    result = AgentRuntime(
        FakeProvider([ModelResponse(content="Explicit retry completed.")]),
        _CountingListGateway(repository, retry_policy, backend_calls),
        state_store=store,
        owner_id="retry-worker",
    ).resume(ready_task, store.get_checkpoint(task.id))

    assert result.status is TaskStatus.COMPLETED
    assert backend_calls == ["retry-call"]
    assert store.get_effect(unknown.id).status is EffectStatus.UNKNOWN
    assert store.get_effect(retry.id).status is EffectStatus.SUCCEEDED
    assert store.get_approval(approval.id).status.value == "consumed"
    assert store.get_recovery_disposition(resolution.disposition.id) == resolution.disposition
