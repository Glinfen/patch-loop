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
from typing import Any

from patchloop.domain import Task, ToolCall, ToolResult
from patchloop.persistence import RuntimeCheckpoint, SQLiteStore
from patchloop.providers import ModelMessage


class FaultPoint(StrEnum):
    INTENT_SUBMITTED = "intent_submitted"
    BEFORE_EXTERNAL_ACTION = "before_external_action"
    AFTER_EXTERNAL_ACTION = "after_external_action"
    RESULT_SUBMITTED = "result_submitted"
    BEFORE_CHECKPOINT_COMMIT = "before_checkpoint_commit"


class FaultInjected(RuntimeError):
    """Raised when a barrier is used without terminating its worker."""


class FaultBarrier:
    """Append an audit record, then optionally terminate at one exact point."""

    def __init__(
        self,
        audit_path: Path,
        *,
        stop_at: FaultPoint | None = None,
        terminate_process: bool = False,
    ) -> None:
        self.audit_path = audit_path
        self.stop_at = stop_at
        self.terminate_process = terminate_process

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


class AuditedExternalAction:
    """A tiny external-writer stand-in used by the legacy crash worker."""

    def __init__(self, barrier: FaultBarrier, target: Path) -> None:
        self.barrier = barrier
        self.target = target

    def run(self, call: ToolCall) -> ToolResult:
        self.barrier.hit(FaultPoint.BEFORE_EXTERNAL_ACTION, call_id=call.id)
        self.barrier.record("external_action_started", call_id=call.id)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        with self.target.open("w", encoding="utf-8") as stream:
            stream.write("external action completed\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.barrier.record("external_action_completed", call_id=call.id)
        self.barrier.hit(FaultPoint.AFTER_EXTERNAL_ACTION, call_id=call.id)
        return ToolResult(
            call_id=call.id,
            tool_name=call.name,
            success=True,
            output="external action completed",
        )


def run_legacy_fault_worker(
    database_path: str,
    target_path: str,
    audit_path: str,
    stop_at: str,
) -> None:
    """Run the pre-SRF execution ordering in a child process.

    This intentionally mirrors the old split calls: external action, then
    result persistence, then checkpoint persistence.
    """

    store = SQLiteStore(Path(database_path))
    task = Task(goal="Characterize legacy recovery", repository=str(Path(target_path).parent))
    store.save_task(task)
    barrier = FaultBarrier(
        Path(audit_path),
        stop_at=FaultPoint(stop_at),
        terminate_process=True,
    )
    call = ToolCall(id="legacy-write-call", name="legacy_external_write")
    barrier.hit(FaultPoint.INTENT_SUBMITTED, task_id=task.id, call_id=call.id)
    result = AuditedExternalAction(barrier, Path(target_path)).run(call)
    store.record_tool_call(task.id, call, result)
    barrier.hit(FaultPoint.RESULT_SUBMITTED, call_id=call.id)
    checkpoint = RuntimeCheckpoint(
        task_id=task.id,
        next_step_index=1,
        messages=[ModelMessage(role="user", content=task.goal)],
    )
    barrier.hit(FaultPoint.BEFORE_CHECKPOINT_COMMIT, call_id=call.id)
    store.save_checkpoint(checkpoint)
