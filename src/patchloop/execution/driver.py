"""Advance-oriented driver for the agent runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from patchloop.domain import Task, TaskOutcome, TaskRuntimeCondition
from patchloop.execution.models import Execution, ExecutionStatus
from patchloop.persistence_contracts import AdvanceResult, AdvanceStatus

if TYPE_CHECKING:
    from patchloop.persistence import RuntimeCheckpoint


@dataclass(frozen=True)
class RuntimeAdvance:
    """Internal result of crossing one durable runtime boundary."""

    status: AdvanceStatus
    task: Task
    checkpoint: RuntimeCheckpoint
    detail: str = ""


AdvanceBoundary = Callable[[], RuntimeAdvance]


def advance_status_for_task(task: Task) -> AdvanceStatus:
    """Project the two-dimensional Task state onto the Runtime port result."""

    if task.runtime_condition is TaskRuntimeCondition.WAITING_FOR_APPROVAL:
        return AdvanceStatus.WAITING
    if task.runtime_condition in {
        TaskRuntimeCondition.PAUSING,
        TaskRuntimeCondition.PAUSED,
    }:
        return AdvanceStatus.PAUSED
    if task.runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED:
        return AdvanceStatus.RECOVERY_REQUIRED
    return {
        TaskOutcome.ACTIVE: AdvanceStatus.PROGRESSED,
        TaskOutcome.COMPLETED: AdvanceStatus.COMPLETED,
        TaskOutcome.FAILED: AdvanceStatus.FAILED,
        TaskOutcome.CANCELLED: AdvanceStatus.CANCELLED,
    }[task.outcome]


class RuntimeDriver:
    """Drive one boundary at a time or loop for legacy synchronous callers."""

    def __init__(self, boundary: AdvanceBoundary) -> None:
        self._boundary = boundary
        self._last: RuntimeAdvance | None = None

    @property
    def last(self) -> RuntimeAdvance | None:
        return self._last

    def advance(self, execution: Execution) -> AdvanceResult:
        outcome = self._boundary()
        self._last = outcome
        return AdvanceResult(
            status=outcome.status,
            execution=execution.model_copy(update={"status": _execution_status(outcome.status)}),
            detail=outcome.detail,
        )

    def run(self) -> Task:
        """Compatibility loop used by AgentRuntime.run/resume."""

        while True:
            outcome = self._boundary()
            self._last = outcome
            if outcome.status is not AdvanceStatus.PROGRESSED:
                return outcome.task


def _execution_status(status: AdvanceStatus) -> ExecutionStatus:
    return {
        AdvanceStatus.PROGRESSED: ExecutionStatus.RUNNING,
        AdvanceStatus.WAITING: ExecutionStatus.WAITING_FOR_APPROVAL,
        AdvanceStatus.PAUSED: ExecutionStatus.PAUSED,
        AdvanceStatus.RECOVERY_REQUIRED: ExecutionStatus.RECOVERY_REQUIRED,
        AdvanceStatus.COMPLETED: ExecutionStatus.COMPLETED,
        AdvanceStatus.FAILED: ExecutionStatus.FAILED,
        AdvanceStatus.CANCELLED: ExecutionStatus.CANCELLED,
    }[status]


__all__ = [
    "AdvanceBoundary",
    "RuntimeAdvance",
    "RuntimeDriver",
    "advance_status_for_task",
]
