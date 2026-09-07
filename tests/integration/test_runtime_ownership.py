"""SRF-03 steps 3-4 guarded writes and runtime lease lifecycle."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from patchloop.domain import AgentStep, StepStatus, Task, TaskStatus, ToolCall, ToolResult
from patchloop.execution.models import Effect, EffectStatus, ExecutionStatus
from patchloop.execution.ownership import ExecutionOwnershipManager, LeasePolicy
from patchloop.memory.models import MemorySource, MemorySourceKind
from patchloop.persistence import RuntimeCheckpoint, SQLiteStore
from patchloop.persistence_contracts import LeaseConflict, LeaseLost
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.runtime import AgentRuntime
from patchloop.tools.base import PermissionLevel, Tool, ToolContext, ToolInputModel
from patchloop.tools.gateway import ToolGateway


class _NoInput(ToolInputModel):
    pass


class _BlockingReadTool(Tool):
    name = "blocking_read"
    description = "Wait before returning."
    input_model = _NoInput
    permission = PermissionLevel.READ

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        time.sleep(0.7)
        return "done"


class _BlockingProvider:
    name = "blocking"

    def __init__(
        self,
        responses: list[ModelResponse],
        delay: float = 0.7,
        on_start: Callable[[], None] | None = None,
    ) -> None:
        self.responses = responses
        self.delay = delay
        self.on_start = on_start
        self.calls = 0

    def complete(self, messages, tools):
        if self.on_start is not None:
            self.on_start()
        time.sleep(self.delay)
        response = self.responses[self.calls]
        self.calls += 1
        return response


class _FailingRenewStore(SQLiteStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.fail_renewals = threading.Event()

    def renew_execution(self, lease_guard, *, now, lease_expires_at):
        if self.fail_renewals.is_set():
            raise LeaseLost(lease_guard.task_id)
        return super().renew_execution(
            lease_guard,
            now=now,
            lease_expires_at=lease_expires_at,
        )


class _ControlledProvider:
    name = "controlled"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        self.started.set()
        if not self.release.wait(2):
            raise TimeoutError("test provider was not released")
        return ModelResponse(tool_calls=[ToolCall(id="late-call", name="blocking_read")])


def _short_policy() -> LeasePolicy:
    return LeasePolicy(
        ttl=timedelta(milliseconds=500),
        heartbeat_interval=timedelta(milliseconds=50),
    )


def test_heartbeat_covers_blocked_provider_and_tool(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    manager = ExecutionOwnershipManager(
        store,
        policy=_short_policy(),
        id_factory=lambda: "execution-1",
    )
    provider = _BlockingProvider(
        [
            ModelResponse(tool_calls=[ToolCall(id="read-1", name="blocking_read")]),
            ModelResponse(content="finished"),
        ]
    )
    gateway = ToolGateway(ToolContext(repository), [_BlockingReadTool()])

    result = AgentRuntime(
        provider,
        gateway,
        state_store=store,
        ownership_manager=manager,
        owner_id="worker-1",
    ).run(Task(id="task-1", goal="Wait safely", repository=str(repository)))

    assert result.status is TaskStatus.COMPLETED
    assert provider.calls == 2
    assert store.get_execution("execution-1").status is ExecutionStatus.RELEASED


def test_acquire_conflict_prevents_provider_call(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    task = store.prepare_task_execution(
        Task(id="task-1", goal="Do not start twice", repository=str(repository))
    )
    first_manager = ExecutionOwnershipManager(store)
    first = first_manager.acquire(
        session_id=task.session_id or "",
        task_id=task.id,
        owner_id="worker-1",
        repository=repository,
        expected_version=task.version,
        workspace_writer=False,
    )
    provider = FakeProvider([ModelResponse(content="must not run")])

    with pytest.raises(LeaseConflict):
        AgentRuntime(
            provider,
            ToolGateway(ToolContext(repository), []),
            state_store=store,
            owner_id="worker-2",
        ).run(store.get_task(task.id))

    assert provider.requests == []
    first_manager.release(first)


def test_renew_failure_stops_before_tool_and_terminal_write(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = _FailingRenewStore(tmp_path / "state.db")
    provider = _BlockingProvider(
        [ModelResponse(tool_calls=[ToolCall(id="read-1", name="blocking_read")])],
        on_start=store.fail_renewals.set,
    )
    manager = ExecutionOwnershipManager(store, policy=_short_policy())

    with pytest.raises(LeaseLost):
        AgentRuntime(
            provider,
            ToolGateway(ToolContext(repository), [_BlockingReadTool()]),
            state_store=store,
            ownership_manager=manager,
            owner_id="worker-1",
        ).run(Task(id="task-1", goal="Lose the lease", repository=str(repository)))

    persisted = store.get_task("task-1")
    assert provider.calls == 1
    assert persisted.status is TaskStatus.RUNNING
    assert store.get_tool_result("task-1", "read-1") is None
    assert store.get_checkpoint("task-1").next_step_index == 0


def test_stale_owner_cannot_write_runtime_or_memory_records(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    task = store.prepare_task_execution(
        Task(id="task-1", goal="Fence writes", repository=str(repository))
    )
    first_manager = ExecutionOwnershipManager(store)
    first = first_manager.acquire(
        session_id=task.session_id or "",
        task_id=task.id,
        owner_id="worker-1",
        repository=repository,
        expected_version=task.version,
        workspace_writer=False,
    )
    first_manager.release(first)
    refreshed = store.get_task(task.id)
    second = ExecutionOwnershipManager(store).acquire(
        session_id=refreshed.session_id or "",
        task_id=refreshed.id,
        owner_id="worker-2",
        repository=repository,
        expected_version=refreshed.version,
        workspace_writer=False,
    )
    stale = first.lease_guard
    step = AgentStep(task_id=task.id, index=0, status=StepStatus.RUNNING)
    source = MemorySource(
        id="source-1",
        task_id=task.id,
        kind=MemorySourceKind.EVENT,
        evidence_hash="a" * 64,
        event_id="event-1",
    )
    call = ToolCall(id="call-1", name="read_file", arguments={"path": "a.py"})
    result = ToolResult(call_id=call.id, tool_name=call.name, success=True, output="ok")
    effect = Effect(
        id="effect-1",
        task_id=task.id,
        step_id="step-1",
        batch_position=0,
        provider_call_id=call.id,
        tool_name=call.name,
    )

    guarded_writes = [
        lambda: store.update_task(
            store.get_task(task.id),
            expected_version=store.get_task(task.id).version,
            lease_guard=stale,
        ),
        lambda: store.record_step(step, lease_guard=stale),
        lambda: store.record_tool_call(task.id, call, result, lease_guard=stale),
        lambda: store.prepare_effects(
            [effect],
            expected_version=store.get_task(task.id).version,
            lease_guard=stale,
        ),
        lambda: store.save_checkpoint(
            RuntimeCheckpoint(task_id=task.id, next_step_index=0, messages=[]),
            lease_guard=stale,
        ),
        lambda: store.record_artifact(task.id, repository / "report.json", lease_guard=stale),
        lambda: store.memory.save_batch(sources=[source], lease_guard=stale),
    ]
    for write in guarded_writes:
        with pytest.raises(LeaseLost):
            write()

    store.assert_execution(second.lease_guard, now=second.execution.updated_at)


def test_cancel_is_persisted_before_runtime_settles_and_stops_actions(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    provider = _ControlledProvider()
    runtime = AgentRuntime(
        provider,
        ToolGateway(ToolContext(repository), [_BlockingReadTool()]),
        state_store=store,
        owner_id="worker-1",
    )
    results: list[Task] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            results.append(
                runtime.run(Task(id="task-1", goal="Cancel safely", repository=str(repository)))
            )
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert provider.started.wait(2)
    requested = store.cancel_task("task-1")
    assert requested.status is TaskStatus.RUNNING
    pending = store.get_pending_control("task-1")
    assert pending is not None
    provider.release.set()
    worker.join(3)

    assert not worker.is_alive()
    assert failures == []
    assert results[0].status is TaskStatus.CANCELLED
    assert store.get_task("task-1").status is TaskStatus.CANCELLED
    assert store.get_pending_control("task-1") is None
    observation = store.get_tool_result("task-1", "late-call")
    assert observation is not None
    assert json.loads(observation.output)["backend_invoked"] is False
    effect = next(
        effect for effect in store.list_effects("task-1") if effect.provider_call_id == "late-call"
    )
    assert effect.status is EffectStatus.CANCELLED
