"""Core domain models shared by the runtime, providers, tools, and storage."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.providers.contracts import ProviderBinding

type AppendOnlyOptimizationVersion = Literal["baseline_v1", "balanced_v1"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SessionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class TaskOutcome(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskRuntimeCondition(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RECOVERY_REQUIRED = "recovery_required"
    ENDED = "ended"


# Short aliases used by the session contract and kept provider-independent.
RuntimeCondition = TaskRuntimeCondition
TaskBusinessOutcome = TaskOutcome


class PromptCacheLayout(StrEnum):
    """Prompt assembly mode used for cache-layout experiments and rollback."""

    LEGACY = "legacy"
    STABLE = "stable"
    APPEND_ONLY = "append_only"


# PPS-09 changes this only after the real-provider acceptance profile passes.
DEFAULT_PROMPT_CACHE_LAYOUT = PromptCacheLayout.LEGACY


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class PlanItem(BaseModel):
    id: str = Field(
        default_factory=lambda: str(uuid4()),
        pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$",
    )
    description: str = Field(min_length=1)
    status: StepStatus = StepStatus.PENDING
    evidence: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    items: list[PlanItem] = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_active_item_count(self) -> Self:
        active_count = sum(item.status is StepStatus.RUNNING for item in self.items)
        if active_count > 1:
            raise ValueError("a plan can have at most one running item")
        return self


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
    TEST_FAILURE = "test_failure"
    SYNTAX_ERROR = "syntax_error"
    COMMAND_FAILURE = "command_failure"


class ValidationRecord(BaseModel):
    tool_name: str
    passed: bool
    error_kind: ErrorKind | None = None
    exit_code: int | None = None
    details: str = ""


class TaskReport(BaseModel):
    summary: str
    changed_files: list[str] = Field(default_factory=list)
    diff: str = ""
    validations: list[ValidationRecord] = Field(default_factory=list)
    tool_calls: int = Field(default=0, ge=0)
    successful_tool_calls: int = Field(default=0, ge=0)
    failed_tool_calls: int = Field(default=0, ge=0)
    replans: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    unknown_model_usage_calls: int = Field(default=0, ge=0)
    model_usage_exact: bool = True
    provider_requests: int = Field(default=0, ge=0)
    provider_attempts: int = Field(default=0, ge=0)
    unknown_usage_attempts: int = Field(default=0, ge=0)
    cost_status: str = Field(default="legacy", pattern=r"^(estimated|unknown|legacy)$")
    reserved_cost_usd: float = Field(default=0.0, ge=0.0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    cache_hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    cache_usage_reported_calls: int = Field(default=0, ge=0)
    cache_usage_unreported_calls: int = Field(default=0, ge=0)
    cache_usage_inconsistent_calls: int = Field(default=0, ge=0)
    cache_write_reported_calls: int = Field(default=0, ge=0)
    context_windows: int = Field(default=0, ge=0)
    context_compactions: int = Field(default=0, ge=0)
    max_context_tokens_used: int = Field(default=0, ge=0)
    truncated_tool_outputs: int = Field(default=0, ge=0)
    working_memory_updates: int = Field(default=0, ge=0)
    working_memory_evictions: int = Field(default=0, ge=0)
    memory_promotions: int = Field(default=0, ge=0)
    max_working_memory_tokens_used: int = Field(default=0, ge=0)
    episodes_created: int = Field(default=0, ge=0)
    episode_recoveries: int = Field(default=0, ge=0)
    last_verified_episode_id: str | None = None
    semantic_facts_created: int = Field(default=0, ge=0)
    semantic_facts_superseded: int = Field(default=0, ge=0)
    semantic_conflicts_rejected: int = Field(default=0, ge=0)
    semantic_duplicates_suppressed: int = Field(default=0, ge=0)
    memory_retrievals: int = Field(default=0, ge=0)
    memory_retrieval_hits: int = Field(default=0, ge=0)
    memory_retrieval_tokens: int = Field(default=0, ge=0)
    memory_events_ingested: int = Field(default=0, ge=0)
    memory_records_written: int = Field(default=0, ge=0)
    memory_compactions: int = Field(default=0, ge=0)
    memory_compression_input_tokens: int = Field(default=0, ge=0)
    memory_compression_output_tokens: int = Field(default=0, ge=0)
    memory_fallbacks: int = Field(default=0, ge=0)
    memory_records_by_kind: dict[str, int] = Field(default_factory=dict)
    memory_records_by_status: dict[str, int] = Field(default_factory=dict)
    memory_stale_hits: int = Field(default=0, ge=0)
    memory_security_filters: int = Field(default=0, ge=0)
    memory_read_duration_ms: float = Field(default=0.0, ge=0.0)
    memory_write_duration_ms: float = Field(default=0.0, ge=0.0)
    memory_compression_duration_ms: float = Field(default=0.0, ge=0.0)
    memory_compression_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    max_memory_context_tokens_used: int = Field(default=0, ge=0)
    max_memory_context_occupancy: float = Field(default=0.0, ge=0.0, le=1.0)
    generated_at: datetime = Field(default_factory=utc_now)


class TaskBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_steps: int = Field(default=20, ge=1, le=1_000)
    max_seconds: float = Field(default=300.0, gt=0)
    max_repeated_actions: int = Field(default=3, ge=2, le=20)
    max_repeated_errors: int = Field(default=3, ge=2, le=20)
    max_tool_failures: int = Field(default=10, ge=0, le=1_000)
    max_replans: int = Field(default=5, ge=0, le=100)
    max_input_tokens: int = Field(default=500_000, ge=1)
    max_output_tokens: int = Field(default=100_000, ge=1)
    max_cost_usd: float = Field(default=5.0, gt=0)
    max_context_tokens: int = Field(default=32_000, ge=256, le=1_000_000)
    max_working_memory_tokens: int = Field(default=2_000, ge=128, le=128_000)
    max_tool_output_chars: int = Field(default=8_000, ge=128, le=1_000_000)
    context_recent_steps: int = Field(default=4, ge=1, le=100)


class TaskExecutionConfig(BaseModel):
    allowed_permissions: list[str] = Field(default_factory=lambda: ["read"])
    non_interactive: bool = True
    sandbox_backend: str = Field(default="docker", pattern=r"^(docker|local)$")
    sandbox_image: str = Field(
        default="patchloop-sandbox:py313",
        pattern=r"^[A-Za-z0-9._/:@-]+$",
    )
    sandbox_workspace_limit_mb: int | None = Field(default=None, ge=64, le=16_384)
    sandbox_workspace_inode_limit: int = Field(default=65_536, ge=1024, le=10_000_000)
    prompt_cache_layout: PromptCacheLayout = DEFAULT_PROMPT_CACHE_LAYOUT
    append_only_optimization: AppendOnlyOptimizationVersion = "baseline_v1"
    project_instructions: str = ""
    cache_epoch: str = Field(default="initial", min_length=1, max_length=128)
    provider: ProviderBinding | None = None

    @model_validator(mode="after")
    def validate_append_only_optimization(self) -> Self:
        if self.sandbox_backend == "local" and self.sandbox_workspace_limit_mb is not None:
            raise ValueError("workspace limits require the Docker sandbox")
        if (
            self.append_only_optimization == "balanced_v1"
            and self.prompt_cache_layout is not PromptCacheLayout.APPEND_ONLY
        ):
            raise ValueError("balanced_v1 optimization requires append_only prompt layout")
        return self


class Task(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    @model_validator(mode="before")
    @classmethod
    def project_legacy_status(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        raw_status = data.get("status", TaskStatus.CREATED)
        status = raw_status if isinstance(raw_status, TaskStatus) else TaskStatus(raw_status)
        if "outcome" not in data:
            data["outcome"] = {
                TaskStatus.CREATED: TaskOutcome.ACTIVE,
                TaskStatus.RUNNING: TaskOutcome.ACTIVE,
                TaskStatus.COMPLETED: TaskOutcome.COMPLETED,
                TaskStatus.FAILED: TaskOutcome.FAILED,
                TaskStatus.CANCELLED: TaskOutcome.CANCELLED,
            }[status]
        if "runtime_condition" not in data:
            data["runtime_condition"] = {
                TaskStatus.CREATED: TaskRuntimeCondition.IDLE,
                TaskStatus.RUNNING: TaskRuntimeCondition.RUNNING,
                TaskStatus.COMPLETED: TaskRuntimeCondition.ENDED,
                TaskStatus.FAILED: TaskRuntimeCondition.ENDED,
                TaskStatus.CANCELLED: TaskRuntimeCondition.ENDED,
            }[status]
        return data

    id: str = Field(
        default_factory=lambda: str(uuid4()),
        pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$",
    )
    goal: str = Field(min_length=1)
    repository: str
    session_id: str | None = Field(default=None, min_length=1)
    status: TaskStatus = TaskStatus.CREATED
    outcome: TaskOutcome = TaskOutcome.ACTIVE
    runtime_condition: TaskRuntimeCondition = TaskRuntimeCondition.IDLE
    version: int = Field(default=1, ge=1)
    budget: TaskBudget = Field(default_factory=TaskBudget)
    execution: TaskExecutionConfig = Field(default_factory=TaskExecutionConfig)
    plan: Plan | None = None
    report: TaskReport | None = None
    result: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_state_dimensions(self) -> Self:
        projected_status = {
            TaskOutcome.ACTIVE: {TaskStatus.CREATED, TaskStatus.RUNNING},
            TaskOutcome.COMPLETED: {TaskStatus.COMPLETED},
            TaskOutcome.FAILED: {TaskStatus.FAILED},
            TaskOutcome.CANCELLED: {TaskStatus.CANCELLED},
        }[self.outcome]
        if self.status not in projected_status:
            raise ValueError(
                f"task status is not a valid compatibility projection for outcome {self.outcome}"
            )
        if self.outcome is not TaskOutcome.ACTIVE and self.runtime_condition not in {
            TaskRuntimeCondition.ENDED,
            TaskRuntimeCondition.RECOVERY_REQUIRED,
        }:
            raise ValueError("terminal task outcome requires ended or recovery_required condition")
        return self

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
        target_outcome = {
            TaskStatus.CREATED: TaskOutcome.ACTIVE,
            TaskStatus.RUNNING: TaskOutcome.ACTIVE,
            TaskStatus.COMPLETED: TaskOutcome.COMPLETED,
            TaskStatus.FAILED: TaskOutcome.FAILED,
            TaskStatus.CANCELLED: TaskOutcome.CANCELLED,
        }[target]
        next_condition = (
            TaskRuntimeCondition.RUNNING
            if target is TaskStatus.RUNNING
            else (
                self.runtime_condition
                if self.runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED
                and target is TaskStatus.CANCELLED
                else TaskRuntimeCondition.ENDED
            )
        )
        object.__setattr__(self, "status", target)
        object.__setattr__(self, "outcome", target_outcome)
        object.__setattr__(self, "runtime_condition", next_condition)
        object.__setattr__(self, "updated_at", utc_now())
        if target is TaskStatus.COMPLETED:
            object.__setattr__(self, "result", message)
        elif target is TaskStatus.FAILED:
            object.__setattr__(self, "error", message)

    def transition_runtime(self, target: TaskRuntimeCondition) -> None:
        allowed = {
            TaskRuntimeCondition.IDLE: {
                TaskRuntimeCondition.RUNNING,
                TaskRuntimeCondition.PAUSED,
                TaskRuntimeCondition.ENDED,
            },
            TaskRuntimeCondition.RUNNING: {
                TaskRuntimeCondition.PAUSING,
                TaskRuntimeCondition.WAITING_FOR_APPROVAL,
                TaskRuntimeCondition.RECOVERY_REQUIRED,
                TaskRuntimeCondition.ENDED,
            },
            TaskRuntimeCondition.PAUSING: {
                TaskRuntimeCondition.PAUSED,
                TaskRuntimeCondition.RECOVERY_REQUIRED,
            },
            TaskRuntimeCondition.PAUSED: {
                TaskRuntimeCondition.RUNNING,
                TaskRuntimeCondition.ENDED,
            },
            TaskRuntimeCondition.WAITING_FOR_APPROVAL: {
                TaskRuntimeCondition.IDLE,
                TaskRuntimeCondition.RUNNING,
                TaskRuntimeCondition.PAUSED,
                TaskRuntimeCondition.RECOVERY_REQUIRED,
                TaskRuntimeCondition.ENDED,
            },
            TaskRuntimeCondition.RECOVERY_REQUIRED: set(),
            TaskRuntimeCondition.ENDED: set(),
        }
        if target not in allowed[self.runtime_condition]:
            raise ValueError(
                f"invalid task runtime transition: {self.runtime_condition} -> {target}"
            )
        object.__setattr__(self, "runtime_condition", target)
        object.__setattr__(self, "updated_at", utc_now())

    def set_outcome(self, target: TaskOutcome, *, message: str | None = None) -> None:
        if target is TaskOutcome.ACTIVE:
            raise ValueError("active is not a terminal outcome transition")
        if self.outcome is not TaskOutcome.ACTIVE:
            raise ValueError(f"invalid task outcome transition: {self.outcome} -> {target}")
        status = {
            TaskOutcome.COMPLETED: TaskStatus.COMPLETED,
            TaskOutcome.FAILED: TaskStatus.FAILED,
            TaskOutcome.CANCELLED: TaskStatus.CANCELLED,
        }[target]
        object.__setattr__(self, "outcome", target)
        object.__setattr__(self, "status", status)
        if self.runtime_condition is not TaskRuntimeCondition.RECOVERY_REQUIRED:
            object.__setattr__(self, "runtime_condition", TaskRuntimeCondition.ENDED)
        object.__setattr__(self, "updated_at", utc_now())
        if target is TaskOutcome.COMPLETED:
            object.__setattr__(self, "result", message)
        elif target is TaskOutcome.FAILED:
            object.__setattr__(self, "error", message)


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    arguments_error: str | None = None


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
    model_response: dict[str, Any] | None = None
    effect_ids: list[str] = Field(default_factory=list)
    consumed_input_sequence: int = Field(default=0, ge=0)
    tool_results: list[ToolResult] = Field(default_factory=list)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
