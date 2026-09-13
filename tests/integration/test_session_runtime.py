"""SRF-05 step 1-2: advance boundaries and the Session application service."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from patchloop.domain import (
    PromptCacheLayout,
    Task,
    TaskBudget,
    TaskExecutionConfig,
    TaskRuntimeCondition,
    TaskStatus,
    ToolCall,
)
from patchloop.execution.approvals import ApprovalService
from patchloop.execution.driver import RuntimeAdvance, RuntimeDriver
from patchloop.execution.models import (
    ApprovalStatus,
    ControlKind,
    ControlRequest,
    Execution,
    ExecutionStatus,
)
from patchloop.persistence import CheckpointSchemaError, RuntimeCheckpoint, SQLiteStore
from patchloop.persistence_contracts import AdvanceStatus, LeaseConflict
from patchloop.prompt_cache import CacheEpoch
from patchloop.providers import FakeProvider, ModelMessage, ModelResponse, ModelUsage
from patchloop.runtime import AgentRuntime
from patchloop.security import RiskLevel
from patchloop.session import SessionService
from patchloop.session.models import Turn, TurnRole
from patchloop.tools import (
    CreateFileTool,
    ListFilesTool,
    PermissionLevel,
    ToolContext,
    ToolGateway,
    ToolPolicy,
    UpdatePlanTool,
)


def _execution(task: Task) -> Execution:
    now = datetime.now(UTC)
    return Execution(
        id="execution-1",
        session_id="session-1",
        task_id=task.id,
        owner_id="worker-1",
        lease_token="lease-token",
        lease_expires_at=now + timedelta(minutes=1),
    )


def _checkpoint(task: Task, next_step_index: int) -> RuntimeCheckpoint:
    return RuntimeCheckpoint(
        task_id=task.id,
        next_step_index=next_step_index,
        messages=[],
    )


def test_advance_crosses_exactly_one_runtime_boundary() -> None:
    task = Task(id="task-1", goal="advance", repository=".")
    checkpoint = _checkpoint(task, 0)
    calls = 0

    def boundary() -> RuntimeAdvance:
        nonlocal calls
        calls += 1
        return RuntimeAdvance(
            status=AdvanceStatus.PROGRESSED,
            task=task,
            checkpoint=checkpoint.model_copy(update={"next_step_index": calls}),
        )

    result = RuntimeDriver(boundary).advance(_execution(task))

    assert calls == 1
    assert result.status is AdvanceStatus.PROGRESSED
    assert result.execution.status is ExecutionStatus.RUNNING


def test_driver_returns_immediately_at_waiting_boundary() -> None:
    task = Task(id="task-1", goal="wait", repository=".")
    checkpoint = _checkpoint(task, 1)
    calls = 0

    def boundary() -> RuntimeAdvance:
        nonlocal calls
        calls += 1
        object.__setattr__(
            task,
            "runtime_condition",
            TaskRuntimeCondition.WAITING_FOR_APPROVAL,
        )
        return RuntimeAdvance(
            status=AdvanceStatus.WAITING,
            task=task,
            checkpoint=checkpoint,
        )

    result = RuntimeDriver(boundary).run()

    assert calls == 1
    assert result.runtime_condition is TaskRuntimeCondition.WAITING_FOR_APPROVAL


def test_legacy_run_drives_one_model_step_at_a_time(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=[ToolCall(name="list_files")]),
            ModelResponse(tool_calls=[ToolCall(name="list_files")]),
            ModelResponse(content="finished"),
        ]
    )
    runtime = _CountingRuntime(
        provider,
        ToolGateway(ToolContext(repository), [ListFilesTool()]),
    )

    result = runtime.run(Task(goal="inspect twice", repository=str(repository)))

    assert result.status is TaskStatus.COMPLETED
    assert runtime.advance_calls == 3
    assert len(provider.requests) == 3


class _CountingRuntime(AgentRuntime):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.advance_calls = 0

    def _advance_once(self, task, state):
        self.advance_calls += 1
        return super()._advance_once(task, state)


def test_session_service_persists_unbound_and_active_task_messages(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    service = SessionService(store)
    session = service.create(str(repository), session_id="session-1")

    first = service.append_message(
        session.id,
        "Initial goal context",
        client_submission_id="message-1",
    )
    duplicate = service.append_message(
        session.id,
        "Initial goal context",
        client_submission_id="message-1",
    )
    task = service.start_task(session.id, "Inspect the repository", task_id="task-1")
    supplement = service.append_message(
        session.id,
        "Also inspect configuration",
        client_submission_id="message-2",
    )

    assert first == duplicate
    assert first.task_id is None
    assert task.session_id == session.id
    assert supplement.task_id == task.id
    assert service.get(session.id).active_task_id == task.id
    assert service.list(str(repository)) == [service.get(session.id)]
    assert [turn.sequence for turn in store.list_turns(session.id)] == [1, 2]


def test_session_service_rejects_second_active_task_and_controls_require_one(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    service = SessionService(store)
    empty = service.create(str(repository), session_id="empty-session")

    with pytest.raises(ValueError, match="no active task"):
        service.request_pause(empty.id)

    session = service.create(str(repository), session_id="session-1")
    task = service.start_task(session.id, "Inspect", task_id="task-1")
    with pytest.raises(LeaseConflict):
        service.start_task(session.id, "Second task", task_id="task-2")

    pause = service.request_pause(
        session.id,
        request_id="pause-1",
    )
    cancel_session = service.create(str(repository), session_id="cancel-session")
    cancel_task = service.start_task(cancel_session.id, "Cancel", task_id="cancel-task")
    cancel = service.request_cancel(cancel_session.id, request_id="cancel-1")

    assert pause.task_id == task.id
    assert pause.kind is ControlKind.PAUSE
    assert cancel.task_id == cancel_task.id
    assert cancel.kind is ControlKind.CANCEL


def test_session_service_resume_runs_new_task_and_allows_close(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    runtime = AgentRuntime(
        FakeProvider([ModelResponse(content="finished")]),
        ToolGateway(ToolContext(repository), []),
        state_store=store,
    )
    service = SessionService(store, runtime)
    session = service.create(str(repository), session_id="session-1")
    service.start_task(session.id, "Finish immediately", task_id="task-1")

    result = service.resume(session.id)
    closed = service.close(session.id)

    assert result.status is TaskStatus.COMPLETED
    assert closed.status.value == "closed"
    with pytest.raises(ValueError, match="closed session"):
        service.append_message(session.id, "too late")


def test_session_service_close_rejects_active_session(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    service = SessionService(SQLiteStore(tmp_path / "state.db"))
    session = service.create(str(repository), session_id="session-1")
    service.start_task(session.id, "Still active", task_id="task-1")

    with pytest.raises(ValueError, match="active task"):
        service.close(session.id)


def test_new_input_during_provider_call_cancels_stale_write(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    provider = _InputBlockingProvider()
    runtime = AgentRuntime(
        provider,
        ToolGateway(
            ToolContext(repository),
            [CreateFileTool()],
            policy=ToolPolicy(
                frozenset({PermissionLevel.WRITE}),
                approval_threshold=None,
                require_plan_for_mutations=False,
            ),
        ),
        state_store=store,
    )
    service = SessionService(store, runtime)
    session = service.create(str(repository), session_id="session-1")
    service.start_task(session.id, "Create stale.txt", task_id="task-1")
    results: list[Task] = []

    worker = threading.Thread(target=lambda: results.append(service.resume(session.id)))
    worker.start()
    assert provider.started.wait(5)
    turn = service.append_message(
        session.id,
        "Do not create stale.txt; stop instead.",
        client_submission_id="constraint-1",
    )
    provider.release.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert results[0].status is TaskStatus.COMPLETED
    assert not (repository / "stale.txt").exists()
    assert not (repository / "also-stale.txt").exists()
    assert {effect.status.value for effect in store.list_effects("task-1")} == {"cancelled"}
    assert len(store.list_effects("task-1")) == 2
    assert len(store.list_tool_results("task-1")) == 2
    assert store.get_checkpoint("task-1").consumed_input_sequence == turn.sequence
    assert store.list_steps("task-1")[0].consumed_input_sequence == 0
    assert any(message.content == turn.content for message in provider.requests[1][0])
    assert {
        message.tool_call_id for message in provider.requests[1][0] if message.role == "tool"
    } == {"stale-write", "second-stale-write"}


class _InputBlockingProvider:
    name = "input-blocking"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((list(messages), list(tools)))
        if len(self.requests) == 1:
            self.started.set()
            if not self.release.wait(5):
                raise TimeoutError("provider was not released")
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="stale-write",
                        name="create_file",
                        arguments={"path": "stale.txt", "content": "stale\n"},
                    ),
                    ToolCall(
                        id="second-stale-write",
                        name="create_file",
                        arguments={"path": "also-stale.txt", "content": "stale\n"},
                    ),
                ]
            )
        return ModelResponse(content="Stopped without applying the stale write.")


def test_effect_claim_atomically_rejects_a_newer_input_revision(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = _InputRaceStore(tmp_path / "state.db")
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="racing-write",
                        name="create_file",
                        arguments={"path": "racing.txt", "content": "stale\n"},
                    )
                ]
            ),
            ModelResponse(content="Observed the newer constraint."),
        ]
    )
    service = SessionService(
        store,
        AgentRuntime(
            provider,
            ToolGateway(
                ToolContext(repository),
                [CreateFileTool()],
                policy=ToolPolicy(
                    frozenset({PermissionLevel.WRITE}),
                    approval_threshold=None,
                    require_plan_for_mutations=False,
                ),
            ),
            state_store=store,
        ),
    )
    session = service.create(str(repository), session_id="session-1")
    service.start_task(session.id, "Create racing.txt", task_id="task-1")

    result = service.resume(session.id)

    assert result.status is TaskStatus.COMPLETED
    assert not (repository / "racing.txt").exists()
    assert store.list_effects("task-1")[0].status.value == "cancelled"
    assert store.injected_turn is not None
    assert store.get_checkpoint("task-1").consumed_input_sequence == store.injected_turn.sequence


class _InputRaceStore(SQLiteStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.injected_turn: Turn | None = None

    def claim_effect(self, effect_id, **kwargs):
        if self.injected_turn is None:
            task = self.get_task(kwargs["lease_guard"].task_id)
            assert task.session_id is not None
            self.injected_turn = self.append_turn(
                Turn(
                    session_id=task.session_id,
                    role=TurnRole.USER,
                    content="Do not execute the racing write.",
                    client_submission_id="claim-race",
                )
            )
        return super().claim_effect(effect_id, **kwargs)


def test_effect_claim_atomically_honors_a_racing_cancel_request(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = _ControlRaceStore(tmp_path / "state.db")
    service = SessionService(
        store,
        AgentRuntime(
            FakeProvider(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="cancelled-write",
                                name="create_file",
                                arguments={"path": "cancelled.txt", "content": "stale\n"},
                            )
                        ]
                    )
                ]
            ),
            ToolGateway(
                ToolContext(repository),
                [CreateFileTool()],
                policy=ToolPolicy(
                    frozenset({PermissionLevel.WRITE}),
                    approval_threshold=None,
                    require_plan_for_mutations=False,
                ),
            ),
            state_store=store,
        ),
    )
    session = service.create(str(repository), session_id="session-1")
    service.start_task(session.id, "Create cancelled.txt", task_id="task-1")

    result = service.resume(session.id)

    assert result.status is TaskStatus.CANCELLED
    assert not (repository / "cancelled.txt").exists()
    assert store.get_pending_control("task-1") is None
    assert store.list_effects("task-1")[0].status.value == "cancelled"


class _ControlRaceStore(SQLiteStore):
    injected = False

    def claim_effect(self, effect_id, **kwargs):
        if not self.injected:
            self.injected = True
            task = self.get_task(kwargs["lease_guard"].task_id)
            self.request_cancel(
                ControlRequest(
                    id="racing-cancel",
                    task_id=task.id,
                    kind=ControlKind.CANCEL,
                ),
                expected_version=task.version,
            )
        return super().claim_effect(effect_id, **kwargs)


def test_recovery_uses_persisted_request_watermark_for_stale_response(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = _CrashAfterResponseStore(tmp_path / "state.db")
    session = SessionService(store).create(str(repository), session_id="session-1")
    task = SessionService(store).start_task(
        session.id,
        "Create recovered-stale.txt",
        task_id="task-1",
    )
    stale_response = ModelResponse(
        tool_calls=[
            ToolCall(
                id="recovered-stale-write",
                name="create_file",
                arguments={"path": "recovered-stale.txt", "content": "stale\n"},
            )
        ],
        usage=ModelUsage(input_tokens=11, output_tokens=3),
    )

    with pytest.raises(KeyboardInterrupt, match="after response persistence"):
        AgentRuntime(
            FakeProvider([stale_response]),
            _write_gateway(repository),
            state_store=store,
        ).run(task)

    persisted_step = store.list_steps(task.id)[0]
    assert persisted_step.consumed_input_sequence == 0
    assert persisted_step.model_response == stale_response.model_dump(mode="json")
    resumed = AgentRuntime(
        FakeProvider(
            [
                ModelResponse(
                    content="Accepted the recovered constraint.",
                    usage=ModelUsage(input_tokens=7, output_tokens=2),
                )
            ]
        ),
        _write_gateway(repository),
        state_store=store,
    ).resume(store.get_task(task.id), store.get_checkpoint(task.id))

    assert resumed.status is TaskStatus.COMPLETED
    assert resumed.report is not None
    assert resumed.report.input_tokens == 18
    assert not (repository / "recovered-stale.txt").exists()
    assert store.list_effects(task.id)[0].status.value == "cancelled"
    assert store.get_checkpoint(task.id).consumed_input_sequence == 1


def _write_gateway(repository: Path) -> ToolGateway:
    return ToolGateway(
        ToolContext(repository),
        [CreateFileTool()],
        policy=ToolPolicy(
            frozenset({PermissionLevel.WRITE}),
            approval_threshold=None,
            require_plan_for_mutations=False,
        ),
    )


class _CrashAfterResponseStore(SQLiteStore):
    crashed = False

    def prepare_effect_batch(self, step, effects, **kwargs):
        prepared = super().prepare_effect_batch(step, effects, **kwargs)
        if not self.crashed:
            self.crashed = True
            task = self.get_task(step.task_id)
            assert task.session_id is not None
            self.append_turn(
                Turn(
                    session_id=task.session_id,
                    role=TurnRole.USER,
                    content="Do not create recovered-stale.txt.",
                    client_submission_id="recovered-constraint",
                )
            )
            raise KeyboardInterrupt("after response persistence")
        return prepared


def test_pause_settles_before_release_and_resume_continues_persisted_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _ManualClock()
    monkeypatch.setattr("patchloop.runtime.monotonic", clock)
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    provider = _PauseBlockingProvider()
    first_service = SessionService(
        store,
        AgentRuntime(
            provider,
            ToolGateway(ToolContext(repository), [ListFilesTool()]),
            state_store=store,
        ),
    )
    session = first_service.create(str(repository), session_id="session-1")
    first_service.start_task(session.id, "Pause and continue", task_id="task-1")
    paused_results: list[Task] = []

    worker = threading.Thread(
        target=lambda: paused_results.append(first_service.resume(session.id))
    )
    worker.start()
    assert provider.started.wait(5)
    clock.value = 5.0
    control = first_service.request_pause(session.id, request_id="pause-runtime")
    assert control.status.value == "requested"
    provider.release.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert paused_results[0].runtime_condition is TaskRuntimeCondition.PAUSED
    paused_elapsed = store.get_checkpoint("task-1").elapsed_seconds
    assert paused_elapsed == pytest.approx(5.0)
    settled = store.get_control_request("pause-runtime")
    assert settled.status.value == "settled"
    clock.value = 1_005.0
    resumed = SessionService(
        store,
        AgentRuntime(
            FakeProvider([ModelResponse(content="continued")]),
            ToolGateway(ToolContext(repository), [ListFilesTool()]),
            state_store=store,
        ),
    ).resume(session.id)

    assert resumed.status is TaskStatus.COMPLETED
    assert store.get_tool_result("task-1", "paused-read") is not None
    assert store.get_checkpoint("task-1").elapsed_seconds == pytest.approx(paused_elapsed)


class _PauseBlockingProvider:
    name = "pause-blocking"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, messages, tools):
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError("provider was not released")
        return ModelResponse(tool_calls=[ToolCall(id="paused-read", name="list_files")])


class _ManualClock:
    value = 0.0

    def __call__(self) -> float:
        return self.value


def test_ordinary_session_resume_stops_at_existing_recovery_requirement(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    session = SessionService(store).create(str(repository), session_id="session-1")
    task = SessionService(store).start_task(session.id, "Inspect", task_id="task-1")
    with pytest.raises(KeyboardInterrupt, match="after Effect claim"):
        AgentRuntime(
            FakeProvider(
                [ModelResponse(tool_calls=[ToolCall(id="unknown-read", name="list_files")])]
            ),
            _CrashAfterClaimGateway(ToolContext(repository), [ListFilesTool()]),
            state_store=store,
        ).run(task)

    first_resume_provider = FakeProvider([])
    recovery = SessionService(
        store,
        AgentRuntime(
            first_resume_provider,
            ToolGateway(ToolContext(repository), [ListFilesTool()]),
            state_store=store,
        ),
    ).resume(session.id)
    assert recovery.runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED
    assert first_resume_provider.requests == []

    blocked_provider = FakeProvider([ModelResponse(content="must not execute")])
    still_blocked = SessionService(
        store,
        AgentRuntime(
            blocked_provider,
            ToolGateway(ToolContext(repository), [ListFilesTool()]),
            state_store=store,
        ),
    ).resume(session.id)

    assert still_blocked.runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED
    assert blocked_provider.requests == []


class _CrashAfterClaimGateway(ToolGateway):
    def execute_claimed(self, task_id, call, *, approval_consumed):
        raise KeyboardInterrupt("after Effect claim")


def test_checkpoint_replays_committed_observation_and_keeps_pending_batch(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = _CrashAfterFirstEffectCommitStore(tmp_path / "state.db")
    session = SessionService(store).create(str(repository), session_id="session-1")
    task = SessionService(store).start_task(session.id, "Inspect twice", task_id="task-1")
    response = ModelResponse(
        tool_calls=[
            ToolCall(id="first-read", name="list_files"),
            ToolCall(id="second-read", name="list_files"),
        ],
        usage=ModelUsage(input_tokens=13, output_tokens=5),
    )

    with pytest.raises(KeyboardInterrupt, match="after first Effect commit"):
        AgentRuntime(
            FakeProvider([response]),
            ToolGateway(ToolContext(repository), [ListFilesTool()]),
            state_store=store,
        ).run(task)

    checkpoint = store.get_checkpoint(task.id)
    effects = store.list_effects(task.id)
    committed_event = next(
        event
        for event in store.list_events(session.id)
        if event.type == "effect.committed" and event.data["effect_id"] == effects[0].id
    )
    assert checkpoint.next_step_index == 0
    assert checkpoint.pending_effect_ids == [effects[1].id]
    assert checkpoint.event_sequence == committed_event.sequence
    assert checkpoint.event_sequence <= store.get_session(session.id).event_sequence
    assert store.get_tool_result(task.id, "first-read") is not None
    assert store.get_tool_result(task.id, "second-read") is None

    resumed = AgentRuntime(
        FakeProvider([ModelResponse(content="Inspection complete.")]),
        ToolGateway(ToolContext(repository), [ListFilesTool()]),
        state_store=store,
    ).resume(store.get_task(task.id), checkpoint)

    assert resumed.status is TaskStatus.COMPLETED
    assert len(store.list_tool_results(task.id)) == 2
    final_checkpoint = store.get_checkpoint(task.id)
    assert final_checkpoint.pending_effect_ids == []
    assert final_checkpoint.memory_manager is not None
    assert final_checkpoint.memory_manager.cursor.processed_event_ids.count("tool:first-read") == 1


class _CrashAfterFirstEffectCommitStore(SQLiteStore):
    crashed = False

    def commit_effect(self, effect, **kwargs):
        committed = super().commit_effect(effect, **kwargs)
        if not self.crashed:
            self.crashed = True
            raise KeyboardInterrupt("after first Effect commit")
        return committed


def test_session_input_stays_after_the_frozen_prompt_cache_prefix(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    provider = FakeProvider([ModelResponse(content="Done")])
    service = SessionService(
        store,
        AgentRuntime(
            provider,
            ToolGateway(ToolContext(repository), []),
            state_store=store,
        ),
    )
    session = service.create(str(repository), session_id="session-1")
    turn = service.append_message(
        session.id,
        "Keep this constraint in the dynamic tail.",
        client_submission_id="constraint-1",
    )
    service.start_task(
        session.id,
        "Respect the stable prefix",
        task_id="task-1",
        execution=TaskExecutionConfig(
            prompt_cache_layout=PromptCacheLayout.STABLE,
            project_instructions="Keep the frozen project rules stable.",
        ),
    )

    result = service.resume(session.id)

    assert result.status is TaskStatus.COMPLETED
    checkpoint = store.get_checkpoint("task-1")
    request_messages = provider.requests[0][0]
    assert checkpoint.consumed_input_sequence == turn.sequence
    assert checkpoint.prompt_prefix_message_count == 3
    assert all(
        message.content != turn.content
        for message in request_messages[: checkpoint.prompt_prefix_message_count]
    )
    assert request_messages[-1].role == "user"
    assert request_messages[-1].content == turn.content


def test_replayed_model_response_usage_is_accounted_exactly_once(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = _CrashAfterUsageCheckpointStore(tmp_path / "state.db")
    session = SessionService(store).create(str(repository), session_id="session-1")
    task = SessionService(store).start_task(session.id, "Answer", task_id="task-1")

    with pytest.raises(KeyboardInterrupt, match="after usage checkpoint"):
        AgentRuntime(
            FakeProvider(
                [
                    ModelResponse(
                        content="Persisted answer",
                        usage=ModelUsage(
                            input_tokens=13,
                            output_tokens=5,
                            cost_usd=0.03,
                        ),
                    )
                ]
            ),
            ToolGateway(ToolContext(repository), []),
            state_store=store,
        ).run(task)

    checkpoint = store.get_checkpoint(task.id)
    assert checkpoint.accounted_model_response_steps == [0]
    assert checkpoint.input_tokens == 13

    replay_provider = FakeProvider([])
    resumed = AgentRuntime(
        replay_provider,
        ToolGateway(ToolContext(repository), []),
        state_store=store,
    ).resume(store.get_task(task.id), checkpoint)

    assert replay_provider.requests == []
    assert resumed.report is not None
    assert resumed.report.input_tokens == 13
    assert resumed.report.output_tokens == 5
    assert resumed.report.cost_usd == pytest.approx(0.03)
    assert resumed.report.model_usage_exact
    assert resumed.report.unknown_model_usage_calls == 0


class _CrashAfterUsageCheckpointStore(SQLiteStore):
    crashed = False

    def save_checkpoint(self, checkpoint, **kwargs):
        super().save_checkpoint(checkpoint, **kwargs)
        if checkpoint.accounted_model_response_steps == [0] and not self.crashed:
            self.crashed = True
            raise KeyboardInterrupt("after usage checkpoint")


def test_in_flight_unpersisted_response_marks_remote_usage_unknown(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    session = SessionService(store).create(str(repository), session_id="session-1")
    task = SessionService(store).start_task(session.id, "Answer", task_id="task-1")

    with pytest.raises(KeyboardInterrupt, match="before response persistence"):
        _CrashBeforeResponsePersistenceRuntime(
            FakeProvider(
                [
                    ModelResponse(
                        content="Lost response",
                        usage=ModelUsage(
                            input_tokens=100,
                            output_tokens=50,
                            cost_usd=0.5,
                        ),
                    )
                ]
            ),
            ToolGateway(ToolContext(repository), []),
            state_store=store,
        ).run(task)

    checkpoint = store.get_checkpoint(task.id)
    assert checkpoint.pending_model_request_step == 0
    assert len(store.list_steps(task.id)) == 1
    assert store.list_steps(task.id)[0].model_response is None

    resumed = AgentRuntime(
        FakeProvider(
            [
                ModelResponse(
                    content="Replacement response",
                    usage=ModelUsage(input_tokens=7, output_tokens=2, cost_usd=0.02),
                )
            ]
        ),
        ToolGateway(ToolContext(repository), []),
        state_store=store,
    ).resume(store.get_task(task.id), checkpoint)

    assert resumed.report is not None
    assert resumed.report.input_tokens == 7
    assert resumed.report.output_tokens == 2
    assert resumed.report.cost_usd == pytest.approx(0.02)
    assert not resumed.report.model_usage_exact
    assert resumed.report.unknown_model_usage_calls == 1
    recovered_checkpoint = store.get_checkpoint(task.id)
    assert recovered_checkpoint.pending_model_request_step is None
    assert recovered_checkpoint.unknown_model_usage_steps == [0]


class _CrashBeforeResponsePersistenceRuntime(AgentRuntime):
    def _prepare_model_response(self, task, step, response):
        raise KeyboardInterrupt("before response persistence")


def test_second_task_inherits_session_dialogue_but_resets_task_state(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="first-plan",
                        name="update_plan",
                        arguments={
                            "items": [{"description": "Create first file", "status": "running"}]
                        },
                    ),
                    ToolCall(
                        id="first-write",
                        name="create_file",
                        arguments={"path": "first.txt", "content": "first\n"},
                    ),
                ],
                usage=ModelUsage(input_tokens=10, output_tokens=2),
            ),
            ModelResponse(
                content="First task finished.",
                usage=ModelUsage(input_tokens=11, output_tokens=3),
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="second-read", name="list_files")],
                usage=ModelUsage(input_tokens=5, output_tokens=1),
            ),
            ModelResponse(
                content="Second task finished.",
                usage=ModelUsage(input_tokens=6, output_tokens=2),
            ),
        ]
    )
    runtime = AgentRuntime(
        provider,
        ToolGateway(
            ToolContext(repository),
            [UpdatePlanTool(), CreateFileTool(), ListFilesTool()],
            policy=ToolPolicy(
                frozenset({PermissionLevel.READ, PermissionLevel.WRITE}),
                approval_threshold=None,
                require_plan_for_mutations=False,
            ),
        ),
        state_store=store,
    )
    service = SessionService(store, runtime)
    session = service.create(str(repository), session_id="session-1")
    global_constraint = service.append_message(
        session.id,
        "Keep prior explicit constraints available.",
        client_submission_id="global-constraint",
    )
    first = service.start_task(session.id, "Create the first file", task_id="task-1")
    task_constraint = service.append_message(
        session.id,
        "The first file must remain available to later tasks.",
        client_submission_id="task-constraint",
    )

    first_result = service.resume(session.id)
    between_tasks = service.append_message(
        session.id,
        "Inspect the existing workspace without changing it.",
        client_submission_id="between-tasks",
    )
    second_budget = TaskBudget(max_steps=2, max_input_tokens=100)
    second = service.start_task(
        session.id,
        "Inspect after the first task",
        task_id="task-2",
        budget=second_budget,
    )
    second_result = service.resume(session.id)

    assert first_result.status is TaskStatus.COMPLETED
    assert second_result.status is TaskStatus.COMPLETED
    assert (repository / "first.txt").read_text(encoding="utf-8") == "first\n"
    second_request = provider.requests[2][0]
    inherited = {message.content for message in second_request}
    assert global_constraint.content in inherited
    assert task_constraint.content in inherited
    assert between_tasks.content in inherited
    assert "First task finished." in inherited
    assert second_result.report is not None
    assert second_result.report.changed_files == []
    assert second_result.report.tool_calls == 1
    assert second_result.report.input_tokens == 11
    assert second_result.plan is None
    assert second_result.budget == second_budget
    assert {effect.task_id for effect in store.list_effects(first.id)} == {first.id}
    assert {effect.task_id for effect in store.list_effects(second.id)} == {second.id}
    second_checkpoint = store.get_checkpoint(second.id)
    assert [result.call_id for result in second_checkpoint.tool_history] == ["second-read"]
    assert second_checkpoint.change_snapshot == {}
    assistant_turns = [
        turn for turn in store.list_turns(session.id) if turn.role is TurnRole.ASSISTANT
    ]
    assert [turn.content for turn in assistant_turns] == [
        "First task finished.",
        "Second task finished.",
    ]


def test_second_task_requires_a_new_one_time_approval(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    policy = ToolPolicy(
        frozenset({PermissionLevel.WRITE}),
        approval_threshold=RiskLevel.MEDIUM,
        require_plan_for_mutations=False,
    )
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="first-approved-write",
                        name="create_file",
                        arguments={"path": "first.txt", "content": "first\n"},
                    )
                ]
            ),
            ModelResponse(content="First approval consumed."),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="second-approved-write",
                        name="create_file",
                        arguments={"path": "second.txt", "content": "second\n"},
                    )
                ]
            ),
            ModelResponse(content="Second approval consumed."),
        ]
    )
    service = SessionService(
        store,
        AgentRuntime(
            provider,
            ToolGateway(ToolContext(repository), [CreateFileTool()], policy=policy),
            state_store=store,
        ),
    )
    session = service.create(str(repository), session_id="session-1")
    first = service.start_task(session.id, "Create first.txt", task_id="task-1")

    waiting_first = service.resume(session.id)
    first_approval = store.list_approvals(first.id)[0]
    ApprovalService(store).decide(
        first_approval.id,
        approved=True,
        source="operator",
        expected_version=first_approval.version,
        workspace_ref=str(repository),
        policy_version=policy.version,
        config_version="1",
    )
    completed_first = service.resume(session.id)
    second = service.start_task(session.id, "Create second.txt", task_id="task-2")
    waiting_second = service.resume(session.id)
    second_approval = store.list_approvals(second.id)[0]

    assert waiting_first.runtime_condition is TaskRuntimeCondition.WAITING_FOR_APPROVAL
    assert completed_first.status is TaskStatus.COMPLETED
    assert waiting_second.runtime_condition is TaskRuntimeCondition.WAITING_FOR_APPROVAL
    assert second_approval.id != first_approval.id
    assert second_approval.effect_id != first_approval.effect_id
    assert store.get_approval(first_approval.id).status is ApprovalStatus.CONSUMED
    assert store.get_approval(second_approval.id).status is ApprovalStatus.PENDING
    assert not (repository / "second.txt").exists()


def test_append_only_resume_without_state_fails_as_checkpoint_schema_error(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    task = Task(
        id="task-1",
        goal="Inspect the repository",
        repository=str(repository),
        execution=TaskExecutionConfig(prompt_cache_layout=PromptCacheLayout.APPEND_ONLY),
    )
    task.transition(TaskStatus.RUNNING)
    epoch = CacheEpoch.bootstrap(
        [
            ModelMessage(role="system", content="static"),
            ModelMessage(role="user", content="Inspect"),
        ],
        prefix_message_count=2,
        epoch_id="initial",
    )
    checkpoint = RuntimeCheckpoint(
        task_id=task.id,
        next_step_index=0,
        messages=[
            ModelMessage(role="system", content="static"),
            ModelMessage(role="user", content="Inspect"),
        ],
        prompt_prefix_message_count=2,
        cache_epoch_state=epoch.snapshot,
    )

    with pytest.raises(CheckpointSchemaError, match="cannot be safely restored"):
        AgentRuntime(
            FakeProvider([]),
            _write_gateway(repository),
        ).resume(task, checkpoint)
