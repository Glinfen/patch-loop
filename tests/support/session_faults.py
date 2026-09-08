"""Parent-controlled fault barriers for SRF-00 recovery characterization.

The helper deliberately keeps an independent, fsynced audit log. A SQLite
row count cannot prove that an external action did or did not happen.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from patchloop.domain import Task, TaskBudget, ToolCall, ToolResult
from patchloop.execution.models import Effect
from patchloop.persistence import RuntimeCheckpoint, SQLiteStore
from patchloop.persistence_contracts import LeaseGuard
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.runtime import AgentRuntime
from patchloop.tools import PermissionLevel, Tool, ToolContext, ToolGateway, ToolPolicy
from patchloop.tools.base import ToolInputModel


class FaultPoint(StrEnum):
    TOOL_DISPATCHED = "tool_dispatched"
    BEFORE_EXTERNAL_ACTION = "before_external_action"
    AFTER_EXTERNAL_ACTION = "after_external_action"
    RESULT_SUBMITTED = "result_submitted"
    BEFORE_CHECKPOINT_COMMIT = "before_checkpoint_commit"


class FaultInjected(RuntimeError):
    """Raised when a barrier is used without terminating its worker."""


class BarrierSignal(Protocol):
    """Small common surface shared by threading and multiprocessing events."""

    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


class FaultBarrier:
    """Append an audit record, then optionally terminate at one exact point."""

    def __init__(
        self,
        audit_path: Path,
        *,
        stop_at: FaultPoint | None = None,
        terminate_process: bool = False,
        reached_signal: BarrierSignal | None = None,
        parent_release: BarrierSignal | None = None,
    ) -> None:
        self.audit_path = audit_path
        self.stop_at = stop_at
        self.terminate_process = terminate_process
        self.reached_signal = reached_signal
        self.parent_release = parent_release

    @classmethod
    def from_environment(cls) -> FaultBarrier:
        raw_point = os.environ.get("PATCHLOOP_FAULT_POINT")
        audit_path = os.environ.get("PATCHLOOP_FAULT_AUDIT")
        if not raw_point or not audit_path:
            return cls(Path(os.devnull))
        return cls(
            Path(audit_path),
            stop_at=FaultPoint(raw_point),
            terminate_process=os.environ.get("PATCHLOOP_FAULT_TERMINATE") == "1",
        )

    def hit(self, point: FaultPoint, **data: Any) -> None:
        self.record("barrier", point=point.value, **data)
        if point != self.stop_at:
            return
        if self.reached_signal is not None:
            self.reached_signal.set()
            if self.parent_release is None:
                raise RuntimeError("a parent-controlled barrier requires a release signal")
            self.parent_release.wait()
            raise FaultInjected(f"parent released fault barrier: {point.value}")
        if self.terminate_process:
            os._exit(97)
        raise FaultInjected(point.value)

    def record(self, kind: str, **data: Any) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "kind": kind,
            "timestamp": datetime.now(UTC).isoformat(),
            **data,
        }
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


class AuditedWriteInput(ToolInputModel):
    path: str


class AuditedExternalWriteTool(Tool):
    """A non-idempotent test tool run through the real gateway boundary."""

    name = "legacy_external_write"
    description = "Append a marker to a repository file for crash characterization."
    input_model = AuditedWriteInput
    permission = PermissionLevel.WRITE

    def __init__(self, barrier: FaultBarrier, marker: str) -> None:
        self.barrier = barrier
        self.marker = marker

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        parsed = AuditedWriteInput.model_validate(arguments)
        target = context.resolve_path(parsed.path, must_exist=False)
        self.barrier.hit(FaultPoint.BEFORE_EXTERNAL_ACTION, marker=self.marker)
        self.barrier.record("external_action_started", marker=self.marker)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            stream.write(f"{self.marker}\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.barrier.record("external_action_completed", marker=self.marker)
        self.barrier.hit(FaultPoint.AFTER_EXTERNAL_ACTION, marker=self.marker)
        return f"external action completed: {self.marker}"


class FaultingToolGateway(ToolGateway):
    """Expose the real gateway dispatch boundary to the parent-controlled barrier."""

    def __init__(self, *args: Any, barrier: FaultBarrier, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.barrier = barrier

    def execute(self, task_id: str, call: ToolCall) -> ToolResult:
        self.barrier.hit(
            FaultPoint.TOOL_DISPATCHED,
            task_id=task_id,
            call_id=call.id,
        )
        return super().execute(task_id, call)


class FaultingSQLiteStore(SQLiteStore):
    """Expose committed result and post-result checkpoint boundaries."""

    def __init__(self, path: Path, barrier: FaultBarrier) -> None:
        self.barrier = barrier
        self.result_submitted = False
        super().__init__(path)

    def commit_effect(
        self,
        effect: Effect,
        *,
        expected_version: int,
        result_ref: str | None,
        observation_ref: str | None,
        lease_guard: LeaseGuard,
        call: ToolCall | None = None,
        result: ToolResult | None = None,
    ) -> Effect:
        committed = super().commit_effect(
            effect,
            expected_version=expected_version,
            result_ref=result_ref,
            observation_ref=observation_ref,
            lease_guard=lease_guard,
            call=call,
            result=result,
        )
        self.result_submitted = True
        self.barrier.hit(
            FaultPoint.RESULT_SUBMITTED,
            task_id=effect.task_id,
            call_id=effect.provider_call_id,
        )
        return committed

    def save_checkpoint(
        self, checkpoint: RuntimeCheckpoint, *, lease_guard: LeaseGuard | None = None
    ) -> None:
        if self.result_submitted:
            self.barrier.hit(
                FaultPoint.BEFORE_CHECKPOINT_COMMIT,
                task_id=checkpoint.task_id,
                next_step_index=checkpoint.next_step_index,
            )
        super().save_checkpoint(checkpoint, lease_guard=lease_guard)


def run_runtime_fault_worker(
    database_path: str,
    target_path: str,
    audit_path: str,
    stop_at: str,
    reached_signal: BarrierSignal | None = None,
    parent_release: BarrierSignal | None = None,
) -> None:
    """Run the pre-SRF ordering through AgentRuntime and ToolGateway."""

    target = Path(target_path)
    repository = target.parent
    repository.mkdir(parents=True, exist_ok=True)
    barrier = FaultBarrier(
        Path(audit_path),
        stop_at=FaultPoint(stop_at),
        terminate_process=reached_signal is None,
        reached_signal=reached_signal,
        parent_release=parent_release,
    )
    store = FaultingSQLiteStore(Path(database_path), barrier)
    call = ToolCall(
        id="legacy-write-call",
        name=AuditedExternalWriteTool.name,
        arguments={"path": target.name},
    )
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=[call]),
            ModelResponse(content="Legacy external write completed."),
        ]
    )
    policy = ToolPolicy(
        allowed_permissions=frozenset({PermissionLevel.READ, PermissionLevel.WRITE}),
        require_plan_for_mutations=False,
    )
    gateway = FaultingToolGateway(
        ToolContext(repository),
        [AuditedExternalWriteTool(barrier, call.id)],
        policy=policy,
        barrier=barrier,
    )
    runtime = AgentRuntime(provider, gateway, state_store=store)
    task = Task(
        id="legacy-running-task",
        goal="Characterize legacy recovery",
        repository=str(repository),
        budget=TaskBudget(max_steps=2),
    )
    runtime.run(task)
