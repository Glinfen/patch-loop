"""Core domain models shared by the runtime, providers, tools, and storage."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ErrorKind(StrEnum):
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    PATH_DENIED = "path_denied"
    EXECUTION_ERROR = "execution_error"
    BUDGET_EXCEEDED = "budget_exceeded"
    PROVIDER_ERROR = "provider_error"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    NO_PROGRESS = "no_progress"


class TaskBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_steps: int = Field(default=20, ge=1, le=1_000)
    max_seconds: float = Field(default=300.0, gt=0)
    max_repeated_actions: int = Field(default=3, ge=2, le=20)


class Task(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    goal: str = Field(min_length=1)
    repository: str
    status: TaskStatus = TaskStatus.CREATED
    budget: TaskBudget = Field(default_factory=TaskBudget)
    result: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def transition(self, target: TaskStatus, *, message: str | None = None) -> None:
        allowed = {
            TaskStatus.CREATED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
            TaskStatus.RUNNING: {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            },
            TaskStatus.COMPLETED: set(),
            TaskStatus.FAILED: set(),
            TaskStatus.CANCELLED: set(),
        }
        if target not in allowed[self.status]:
            raise ValueError(f"invalid task transition: {self.status} -> {target}")
        self.status = target
        self.updated_at = utc_now()
        if target is TaskStatus.COMPLETED:
            self.result = message
        elif target is TaskStatus.FAILED:
            self.error = message


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    call_id: str
    tool_name: str
    success: bool
    output: str = ""
    error_kind: ErrorKind | None = None
    duration_ms: float = Field(default=0.0, ge=0)


class AgentStep(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    index: int = Field(ge=0)
    status: StepStatus = StepStatus.PENDING
    decision: str | None = None
    tool_results: list[ToolResult] = Field(default_factory=list)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
