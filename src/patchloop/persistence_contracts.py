"""Backend-neutral SRF-01 persistence contracts and a deterministic fake store."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import wraps
from threading import RLock
from typing import Concatenate, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.domain import (
    AgentStep,
    ErrorKind,
    SessionStatus,
    Task,
    TaskOutcome,
    TaskRuntimeCondition,
    TaskStatus,
    ToolCall,
    ToolResult,
)
from patchloop.events import (
    SessionEvent,
    effect_commit_event_id,
    journal_event_id,
    lease_owner_summary,
)
from patchloop.execution.approvals import (
    ApprovalResolution,
    evaluate_claim_policy,
    scope_approval,
    supersede_approval,
    validate_grant_consumption,
)
from patchloop.execution.models import (
    Approval,
    ApprovalStatus,
    ControlRequest,
    ControlStatus,
    Effect,
    EffectStatus,
    Execution,
    ExecutionStatus,
    RecoveryDisposition,
    RecoveryDispositionKind,
    WorkspaceLease,
)
from patchloop.execution.policy import (
    ActionDescriptor,
    ApprovalGrant,
    ApprovalScopeKind,
    PolicyEvaluation,
    PolicyRule,
)
from patchloop.execution.recovery import validate_recovery_resolution
from patchloop.providers.base import ModelResponse, ModelUsage, ProviderRequestPurpose
from patchloop.sandbox import ManagedCommandIdentity, ManagedCommandStatus
from patchloop.security import SecretRedactor
from patchloop.session.models import Session, SessionCheckpoint, Turn

_T = TypeVar("_T")


class ContractError(RuntimeError):
    """Base class for structured persistence boundary errors."""

    code = "contract_error"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.details = details


class StaleVersion(ContractError):
    code = "stale_version"

    def __init__(self, entity_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"stale version for {entity_id}: expected {expected}, actual {actual}",
            entity_id=entity_id,
            expected=expected,
            actual=actual,
        )
        self.entity_id = entity_id
        self.expected = expected
        self.actual = actual


class LeaseConflict(ContractError):
    code = "lease_conflict"

    def __init__(self, resource_id: str, owner_id: str | None = None) -> None:
        super().__init__(
            f"execution lease is already held for {resource_id}",
            resource_id=resource_id,
            owner_id=owner_id,
        )
        self.resource_id = resource_id
        self.owner_id = owner_id


class LeaseLost(ContractError):
    code = "lease_lost"

    def __init__(self, resource_id: str) -> None:
        super().__init__(
            f"execution lease is no longer valid for {resource_id}", resource_id=resource_id
        )
        self.resource_id = resource_id


class ApprovalConflict(ContractError):
    code = "approval_conflict"

    def __init__(self, approval_id: str, actual: str, requested: str) -> None:
        super().__init__(
            f"approval {approval_id} already has decision {actual}; cannot apply {requested}",
            approval_id=approval_id,
            actual=actual,
            requested=requested,
        )
        self.approval_id = approval_id
        self.actual = actual
        self.requested = requested


class EffectIdentityConflict(ContractError):
    code = "effect_identity_conflict"

    def __init__(self, effect_id: str, identity_key: tuple[str, str, int]) -> None:
        super().__init__(
            f"effect identity conflict for {effect_id}",
            effect_id=effect_id,
            identity_key=identity_key,
        )
        self.effect_id = effect_id
        self.identity_key = identity_key


class RecoveryRequired(ContractError):
    code = "recovery_required"

    def __init__(self, task_id: str, effect_id: str | None = None) -> None:
        super().__init__(
            f"recovery disposition is required for task {task_id}",
            task_id=task_id,
            effect_id=effect_id,
        )
        self.task_id = task_id
        self.effect_id = effect_id


class SubmissionConflict(ContractError):
    code = "submission_conflict"

    def __init__(self, session_id: str, client_submission_id: str) -> None:
        super().__init__(
            f"client submission {client_submission_id} conflicts in session {session_id}",
            session_id=session_id,
            client_submission_id=client_submission_id,
        )
        self.session_id = session_id
        self.client_submission_id = client_submission_id


class InputRevisionConflict(ContractError):
    code = "input_revision_conflict"

    def __init__(self, session_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"input revision changed for {session_id}: expected {expected}, actual {actual}",
            session_id=session_id,
            expected=expected,
            actual=actual,
        )
        self.session_id = session_id
        self.expected = expected
        self.actual = actual


class ControlRequested(ContractError):
    code = "control_requested"

    def __init__(self, task_id: str, request_id: str) -> None:
        super().__init__(
            f"control request {request_id} is pending for task {task_id}",
            task_id=task_id,
            request_id=request_id,
        )
        self.task_id = task_id
        self.request_id = request_id


@dataclass(frozen=True)
class LeaseGuard:
    """Opaque execution fencing data required by execution-owned writes."""

    execution_id: str
    task_id: str
    token: str
    generation: int
    owner_id: str


@dataclass(frozen=True)
class WorkspaceLeaseGuard:
    """Opaque fencing data for a workspace writer."""

    workspace_id: str
    execution_id: str
    token: str
    generation: int
    owner_id: str


class ProviderRequestStatus(StrEnum):
    PENDING = "pending"
    RESPONSE_READY = "response_ready"
    COMPLETED = "completed"
    INVALIDATED = "invalidated"


class ProviderAttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ProviderUsageStatus(StrEnum):
    REPORTED = "reported"
    UNKNOWN = "unknown"
    UNREPORTED = "unreported"


class ProviderRequestRecord(BaseModel):
    """Durable identity and completion state for one logical provider request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=128)
    purpose: ProviderRequestPurpose
    step_index: int = Field(ge=0)
    epoch_generation: int = Field(default=0, ge=0)
    input_revision: int = Field(default=0, ge=0)
    binding_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: ProviderRequestStatus = ProviderRequestStatus.PENDING
    response: ModelResponse | None = None
    response_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    usage: ModelUsage | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def logical_key(self) -> tuple[str, ProviderRequestPurpose, int, int, int]:
        return (
            self.task_id,
            self.purpose,
            self.step_index,
            self.epoch_generation,
            self.input_revision,
        )

    @model_validator(mode="after")
    def validate_response_state(self) -> ProviderRequestRecord:
        if self.response is None and self.response_sha256 is not None:
            raise ValueError("provider response digest requires a stored response")
        if self.response is not None and self.response_sha256 is None:
            raise ValueError("stored provider response requires an integrity digest")
        if (
            self.status in {ProviderRequestStatus.RESPONSE_READY, ProviderRequestStatus.COMPLETED}
            and self.response is None
        ):
            raise ValueError("completed provider request requires its full response")
        if self.status is ProviderRequestStatus.INVALIDATED and self.response is not None:
            raise ValueError("invalidated provider request cannot retain a replayable response")
        return self


class ProviderAttemptOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ProviderAttemptStatus
    error_kind: str | None = Field(default=None, max_length=128)
    safe_message: str | None = Field(default=None, max_length=2_000)
    usage_status: ProviderUsageStatus = ProviderUsageStatus.UNKNOWN
    usage: ModelUsage | None = None
    request_sent: bool | None = None

    @model_validator(mode="after")
    def validate_terminal_outcome(self) -> ProviderAttemptOutcome:
        if self.status not in {
            ProviderAttemptStatus.FAILED,
            ProviderAttemptStatus.CANCELLED,
            ProviderAttemptStatus.INTERRUPTED,
        }:
            raise ValueError("provider attempt outcome must be terminal and unsuccessful")
        return self


class ProviderAttemptRecord(BaseModel):
    """One network attempt, including failed/unknown remote usage states."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=1, max_length=256)
    execution_id: str = Field(min_length=1, max_length=128)
    ordinal: int = Field(ge=1)
    status: ProviderAttemptStatus = ProviderAttemptStatus.RUNNING
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    error_kind: str | None = Field(default=None, max_length=128)
    safe_message: str | None = Field(default=None, max_length=2_000)
    usage_status: ProviderUsageStatus = ProviderUsageStatus.UNREPORTED
    usage: ModelUsage | None = None
    request_sent: bool | None = None
    budget_reservation_usd: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def validate_terminal_state(self) -> ProviderAttemptRecord:
        if self.status is ProviderAttemptStatus.RUNNING and self.finished_at is not None:
            raise ValueError("running provider attempt cannot have a finish time")
        if self.status is not ProviderAttemptStatus.RUNNING and self.finished_at is None:
            raise ValueError("terminal provider attempt requires a finish time")
        return self


class ProviderRequestConflict(ContractError):
    code = "provider_request_conflict"

    def __init__(
        self, request_id: str, message: str = "provider request identity conflict"
    ) -> None:
        super().__init__(message, request_id=request_id)
        self.request_id = request_id


class AdvanceStatus(StrEnum):
    PROGRESSED = "progressed"
    WAITING = "waiting"
    PAUSED = "paused"
    RECOVERY_REQUIRED = "recovery_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class AdvanceResult:
    """Stable Runtime port result; callers need not parse exception text."""

    status: AdvanceStatus
    execution: Execution
    detail: str = ""


class SessionStore(Protocol):
    def create_session(self, session: Session) -> Session: ...

    def list_sessions(self, workspace_ref: str | None = None) -> list[Session]: ...

    def get_session(self, session_id: str) -> Session: ...

    def append_turn(self, turn: Turn, *, expected_version: int | None = None) -> Turn: ...

    def list_turns(self, session_id: str, *, after_sequence: int = 0) -> list[Turn]: ...

    def append_event(
        self, event: SessionEvent, *, expected_sequence: int | None = None
    ) -> SessionEvent: ...

    def list_events(self, session_id: str, *, after_sequence: int = 0) -> list[SessionEvent]: ...

    def start_task(self, session_id: str, task: Task, *, expected_version: int) -> Task: ...

    def request_pause(
        self, request: ControlRequest, *, expected_version: int
    ) -> ControlRequest: ...

    def request_cancel(
        self, request: ControlRequest, *, expected_version: int
    ) -> ControlRequest: ...

    def close_session(self, session_id: str, *, expected_version: int) -> Session: ...


class RuntimeStore(Protocol):
    def replace_effect_approval(
        self,
        effect_id: str,
        replacement: Effect,
        approval: Approval,
        *,
        expected_version: int,
        lease_guard: LeaseGuard,
    ) -> tuple[Effect, Approval, Task]: ...

    def create_task(self, task: Task) -> Task: ...

    def get_task(self, task_id: str) -> Task: ...

    def update_task(
        self,
        task: Task,
        *,
        expected_version: int,
        lease_guard: LeaseGuard | None = None,
    ) -> Task: ...

    def commit_tool_result(
        self,
        task_id: str,
        call: ToolCall,
        result: ToolResult,
        *,
        expected_version: int | None = None,
        lease_guard: LeaseGuard | None = None,
    ) -> ToolResult: ...

    def get_tool_result(self, task_id: str, call_id: str) -> ToolResult | None: ...

    def list_tool_results(self, task_id: str) -> list[ToolResult]: ...

    def get_execution(self, execution_id: str) -> Execution: ...

    def list_executions(self, task_id: str) -> list[Execution]: ...

    def claim_execution(
        self,
        execution: Execution,
        *,
        expected_version: int | None = None,
        now: datetime | None = None,
    ) -> Execution: ...

    def renew_execution(
        self, lease_guard: LeaseGuard, *, now: datetime, lease_expires_at: datetime
    ) -> Execution: ...

    def release_execution(self, lease_guard: LeaseGuard, *, now: datetime) -> None: ...

    def assert_execution(self, lease_guard: LeaseGuard, *, now: datetime) -> Execution: ...

    def acquire_workspace_writer(
        self, lease: WorkspaceLease, *, lease_guard: LeaseGuard, now: datetime
    ) -> WorkspaceLease: ...

    def renew_workspace_writer(
        self,
        lease_guard: WorkspaceLeaseGuard,
        *,
        now: datetime,
        lease_expires_at: datetime,
    ) -> WorkspaceLease: ...

    def release_workspace_writer(
        self, lease_guard: WorkspaceLeaseGuard, *, now: datetime
    ) -> None: ...

    def assert_workspace_writer(
        self, lease_guard: WorkspaceLeaseGuard, *, now: datetime
    ) -> WorkspaceLease: ...

    def prepare_effects(
        self,
        effects: Sequence[Effect],
        *,
        expected_version: int,
        lease_guard: LeaseGuard | None = None,
    ) -> list[Effect]: ...

    def prepare_effect_batch(
        self,
        step: AgentStep,
        effects: Sequence[Effect],
        *,
        expected_version: int,
        lease_guard: LeaseGuard | None = None,
        provider_request_id: str | None = None,
    ) -> list[Effect]: ...

    def begin_provider_request(
        self, record: ProviderRequestRecord, *, lease_guard: LeaseGuard
    ) -> ProviderRequestRecord: ...

    def begin_provider_attempt(
        self, record: ProviderAttemptRecord, *, lease_guard: LeaseGuard
    ) -> ProviderAttemptRecord: ...

    def finish_provider_attempt(
        self,
        attempt_id: str,
        outcome: ProviderAttemptOutcome,
        *,
        lease_guard: LeaseGuard,
    ) -> ProviderAttemptRecord: ...

    def commit_provider_response(
        self,
        request_id: str,
        response: ModelResponse,
        *,
        lease_guard: LeaseGuard,
    ) -> ProviderRequestRecord: ...

    def get_provider_request(self, request_id: str) -> ProviderRequestRecord: ...

    def list_provider_attempts(self, request_id: str) -> list[ProviderAttemptRecord]: ...

    def invalidate_provider_request(
        self, request_id: str, *, lease_guard: LeaseGuard
    ) -> ProviderRequestRecord: ...

    def decide_approval(
        self, approval: Approval, *, expected_version: int | None = None
    ) -> Approval: ...

    def request_effect_approval(
        self,
        effect_id: str,
        approval: Approval,
        *,
        expected_version: int,
        lease_guard: LeaseGuard,
    ) -> tuple[Effect, Approval, Task]: ...

    def resolve_effect_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        source: str,
        expected_version: int,
        workspace_ref: str,
        policy_version: str,
        config_version: str,
        scope_kind: ApprovalScopeKind = ApprovalScopeKind.ONCE,
        expires_at: datetime | None = None,
        reason: str | None = None,
    ) -> ApprovalResolution: ...

    def claim_effect(
        self,
        effect_id: str,
        *,
        expected_version: int,
        lease_guard: LeaseGuard,
        effect_fingerprint: str | None = None,
        workspace_ref: str | None = None,
        policy_version: str | None = None,
        config_version: str | None = None,
        policy_decision: str | None = None,
        configured_rules: tuple[PolicyRule, ...] = (),
        current_evaluation: PolicyEvaluation | None = None,
        expected_input_sequence: int | None = None,
    ) -> Effect: ...

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
    ) -> Effect: ...

    def settle_unexecuted_effect(
        self,
        effect_id: str,
        *,
        status: EffectStatus,
        expected_version: int,
        call: ToolCall,
        result: ToolResult,
        lease_guard: LeaseGuard,
    ) -> Effect: ...

    def mark_effect_unknown(
        self,
        effect_id: str,
        *,
        expected_version: int,
        evidence: dict[str, object],
        lease_guard: LeaseGuard,
    ) -> tuple[Effect, Task]: ...

    def request_control(
        self, request: ControlRequest, *, expected_version: int
    ) -> ControlRequest: ...

    def settle_control(
        self,
        request_id: str,
        *,
        status: ControlStatus,
        expected_version: int,
        cleanup_info: str | None = None,
    ) -> ControlRequest: ...

    def get_pending_control(self, task_id: str) -> ControlRequest | None: ...

    def get_control_request(self, request_id: str) -> ControlRequest: ...

    def resolve_recovery(
        self,
        disposition: RecoveryDisposition,
        *,
        task_id: str,
        expected_version: int,
        confirmed_call: ToolCall | None = None,
        confirmed_result: ToolResult | None = None,
        retry_effect: Effect | None = None,
        retry_approval: Approval | None = None,
    ) -> Task: ...

    def get_recovery_disposition(self, disposition_id: str) -> RecoveryDisposition: ...

    def get_recovery_disposition_for_effect(self, effect_id: str) -> RecoveryDisposition | None: ...

    def commit_checkpoint(
        self,
        checkpoint: SessionCheckpoint,
        *,
        expected_version: int,
        lease_guard: LeaseGuard,
    ) -> SessionCheckpoint: ...

    def get_session_checkpoint(self, task_id: str) -> SessionCheckpoint: ...


class PolicyStore(Protocol):
    def put_policy_rule(self, rule: PolicyRule) -> PolicyRule: ...

    def list_policy_rules(self, workspace_ref: str) -> list[PolicyRule]: ...

    def get_grant(self, grant_id: str) -> ApprovalGrant: ...

    def list_grants(self, scope_id: str) -> list[ApprovalGrant]: ...

    def consume_grant(
        self,
        grant_id: str,
        descriptor: ActionDescriptor,
        *,
        expected_version: int,
        policy_version: str,
        config_version: str,
        configured_rules: tuple[PolicyRule, ...] = (),
    ) -> ApprovalGrant: ...

    def revoke_grant(
        self, grant_id: str, *, source: str, reason: str | None = None, expected_version: int
    ) -> ApprovalGrant: ...


class Store(SessionStore, RuntimeStore, PolicyStore, Protocol):
    """Combined backend boundary for a composition root."""

    pass


class RuntimePort(Protocol):
    def advance(self, execution: Execution) -> AdvanceResult: ...


class EffectPort(Protocol):
    def prepare(self, execution: Execution, effects: Sequence[Effect]) -> list[Effect]: ...

    def execute(self, execution: Execution, effect: Effect) -> Effect: ...

    def reconcile(self, execution: Execution, effect: Effect) -> Effect: ...


def _policy_atomic[**P, T](
    method: Callable[Concatenate[FakeStore, P], T],
) -> Callable[Concatenate[FakeStore, P], T]:
    @wraps(method)
    def locked(self: FakeStore, /, *args: P.args, **kwargs: P.kwargs) -> T:
        with self._policy_lock:
            return method(self, *args, **kwargs)

    return locked


class FakeStore:
    """Small in-memory store implementing SRF-01 semantics for service tests."""

    def __init__(self) -> None:
        self._policy_lock = RLock()
        self.sessions: dict[str, Session] = {}
        self.tasks: dict[str, Task] = {}
        self.turns: dict[str, list[Turn]] = {}
        self.executions: dict[str, Execution] = {}
        self.workspace_leases: dict[str, WorkspaceLease] = {}
        self.effects: dict[str, Effect] = {}
        self.steps: dict[tuple[str, int], AgentStep] = {}
        self.approvals: dict[str, Approval] = {}
        self.grants: dict[str, ApprovalGrant] = {}
        self.policy_rules: dict[str, PolicyRule] = {}
        self.controls: dict[str, ControlRequest] = {}
        self.recoveries: dict[str, RecoveryDisposition] = {}
        self.checkpoints: dict[str, SessionCheckpoint] = {}
        self.events: dict[str, list[SessionEvent]] = {}
        self.tool_results: dict[tuple[str, str], tuple[ToolCall, ToolResult]] = {}
        self.managed_commands: dict[str, ManagedCommandIdentity] = {}
        self.provider_requests: dict[str, ProviderRequestRecord] = {}
        self.provider_attempts: dict[str, ProviderAttemptRecord] = {}

    @staticmethod
    def _copy(value: _T) -> _T:
        return deepcopy(value)

    @staticmethod
    def _check_version(entity_id: str, actual: int, expected: int | None) -> None:
        if expected is not None and actual != expected:
            raise StaleVersion(entity_id, expected, actual)

    def create_session(self, session: Session) -> Session:
        if session.id in self.sessions:
            raise ValueError(f"session already exists: {session.id}")
        self.sessions[session.id] = self._copy(session)
        self.turns[session.id] = []
        self.events[session.id] = []
        return self._journal(
            session,
            event_id=journal_event_id("session.created", session.id, session.version),
            event_type="session.created",
            data={"workspace_ref": session.workspace_ref},
        )

    def list_sessions(self, workspace_ref: str | None = None) -> list[Session]:
        values: list[Session] = list(self.sessions.values())
        if workspace_ref is not None:
            values = [session for session in values if session.workspace_ref == workspace_ref]
        return [self._copy(session) for session in values]

    def get_session(self, session_id: str) -> Session:
        try:
            return self._copy(self.sessions[session_id])
        except KeyError as exc:
            raise KeyError(f"session not found: {session_id}") from exc

    def list_turns(self, session_id: str, *, after_sequence: int = 0) -> list[Turn]:
        self.get_session(session_id)
        return [
            self._copy(turn)
            for turn in self.turns.get(session_id, [])
            if turn.sequence > after_sequence
        ]

    def append_event(
        self, event: SessionEvent, *, expected_sequence: int | None = None
    ) -> SessionEvent:
        session = self.get_session(event.session_id)
        events = self.events.setdefault(session.id, [])
        for existing in events:
            if existing.id != event.id:
                continue
            comparable = event.model_copy(update={"sequence": existing.sequence})
            if existing == comparable:
                return self._copy(existing)
            raise ValueError(f"event already exists with different content: {event.id}")
        self._check_version(session.id, session.event_sequence, expected_sequence)
        assigned = event.model_copy(update={"sequence": session.event_sequence + 1})
        events.append(assigned)
        self.sessions[session.id] = session.model_copy(update={"event_sequence": assigned.sequence})
        return self._copy(assigned)

    def list_events(self, session_id: str, *, after_sequence: int = 0) -> list[SessionEvent]:
        self.get_session(session_id)
        return [
            self._copy(event)
            for event in self.events.get(session_id, [])
            if event.sequence > after_sequence
        ]

    def close_session(self, session_id: str, *, expected_version: int) -> Session:
        session = self.get_session(session_id)
        self._check_version(session.id, session.version, expected_version)
        if session.active_task_id is not None:
            raise ValueError("cannot close a session with an active task")
        closed = session.model_copy(
            update={"status": SessionStatus.CLOSED, "version": session.version + 1}
        )
        return self._journal(
            closed,
            event_id=journal_event_id("session.closed", closed.id, closed.version),
            event_type="session.closed",
        )

    def request_pause(self, request: ControlRequest, *, expected_version: int) -> ControlRequest:
        if request.kind.value != "pause":
            raise ValueError("pause request must use the pause control kind")
        return self.request_control(request, expected_version=expected_version)

    def request_cancel(self, request: ControlRequest, *, expected_version: int) -> ControlRequest:
        if request.kind.value != "cancel":
            raise ValueError("cancel request must use the cancel control kind")
        return self.request_control(request, expected_version=expected_version)

    def append_turn(self, turn: Turn, *, expected_version: int | None = None) -> Turn:
        session = self.get_session(turn.session_id)
        if session.status is SessionStatus.CLOSED:
            raise ValueError("cannot append a turn to a closed session")
        turns = self.turns.setdefault(session.id, [])
        if turn.client_submission_id is not None:
            for existing in turns:
                if existing.client_submission_id != turn.client_submission_id:
                    continue
                if (
                    existing.content == turn.content
                    and existing.role is turn.role
                    and existing.resource_refs == turn.resource_refs
                    and (turn.task_id is None or existing.task_id == turn.task_id)
                ):
                    return self._copy(existing)
                raise SubmissionConflict(session.id, turn.client_submission_id)
        self._check_version(session.id, session.version, expected_version)
        if turn.task_id is not None:
            task = self.get_task(turn.task_id)
            if task.session_id != session.id:
                raise ValueError("turn task does not belong to its session")
        assigned = turn.model_copy(
            update={"sequence": len(turns) + 1, "task_id": turn.task_id or session.active_task_id}
        )
        turns.append(assigned)
        self._journal(
            session.model_copy(update={"version": session.version + 1}),
            event_id=journal_event_id("turn.appended", assigned.id, assigned.sequence),
            event_type="turn.appended",
            task_id=assigned.task_id,
            data={"turn_id": assigned.id, "role": assigned.role.value},
        )
        return self._copy(assigned)

    def create_task(self, task: Task) -> Task:
        if task.id in self.tasks:
            raise ValueError(f"task already exists: {task.id}")
        if task.session_id is not None:
            self.get_session(task.session_id)
        self.tasks[task.id] = self._copy(task)
        if task.session_id is not None:
            self._journal(
                self.get_session(task.session_id),
                event_id=journal_event_id("task.created", task.id, task.version),
                event_type="task.created",
                task_id=task.id,
            )
        return self._copy(task)

    def get_task(self, task_id: str) -> Task:
        try:
            return self._copy(self.tasks[task_id])
        except KeyError as exc:
            raise KeyError(f"task not found: {task_id}") from exc

    def get_execution(self, execution_id: str) -> Execution:
        try:
            return self._copy(self.executions[execution_id])
        except KeyError as exc:
            raise KeyError(f"execution not found: {execution_id}") from exc

    def list_executions(self, task_id: str) -> list[Execution]:
        return sorted(
            [
                self._copy(execution)
                for execution in self.executions.values()
                if execution.task_id == task_id
            ],
            key=lambda execution: (execution.generation, execution.created_at, execution.id),
        )

    def get_effect(self, effect_id: str) -> Effect:
        try:
            return self._copy(self.effects[effect_id])
        except KeyError as exc:
            raise KeyError(f"effect not found: {effect_id}") from exc

    def list_effects(self, task_id: str) -> list[Effect]:
        return sorted(
            [self._copy(effect) for effect in self.effects.values() if effect.task_id == task_id],
            key=lambda effect: (effect.step_id, effect.batch_position),
        )

    @_policy_atomic
    def get_approval(self, approval_id: str) -> Approval:
        try:
            approval = self.approvals[approval_id]
            expired = approval.expire()
            if expired != approval:
                self.approvals[approval_id] = self._copy(expired)
                self._journal_approval(expired, "approval.expired")
            return self._copy(expired)
        except KeyError as exc:
            raise KeyError(f"approval not found: {approval_id}") from exc

    @_policy_atomic
    def list_approvals(self, task_id: str) -> list[Approval]:
        effect_ids = {effect.id for effect in self.effects.values() if effect.task_id == task_id}
        return sorted(
            [
                self.get_approval(approval.id)
                for approval in self.approvals.values()
                if approval.effect_id in effect_ids
            ],
            key=lambda approval: (approval.created_at, approval.id),
        )

    def list_steps(self, task_id: str) -> list[AgentStep]:
        return [
            self._copy(step)
            for (candidate_task_id, _), step in sorted(self.steps.items())
            if candidate_task_id == task_id
        ]

    def get_checkpoint(self, task_id: str) -> SessionCheckpoint:
        try:
            return self._copy(self.checkpoints[task_id])
        except KeyError as exc:
            raise KeyError(f"checkpoint not found: {task_id}") from exc

    def get_session_checkpoint(self, task_id: str) -> SessionCheckpoint:
        return self.get_checkpoint(task_id)

    def start_task(self, session_id: str, task: Task, *, expected_version: int) -> Task:
        session = self.get_session(session_id)
        self._check_version(session.id, session.version, expected_version)
        if session.status is SessionStatus.CLOSED:
            raise ValueError("cannot start a task in a closed session")
        if session.active_task_id is not None:
            raise LeaseConflict(session_id, session.active_task_id)
        if task.outcome is not TaskOutcome.ACTIVE:
            raise ValueError("a new session task must have an active outcome")
        bound = task.model_copy(update={"session_id": session_id})
        if bound.id in self.tasks:
            raise ValueError(f"task already exists: {bound.id}")
        self.tasks[bound.id] = self._copy(bound)
        self._journal(
            session.model_copy(update={"active_task_id": bound.id, "version": session.version + 1}),
            event_id=journal_event_id("task.started", bound.id, bound.version),
            event_type="task.started",
            task_id=bound.id,
            data={"outcome": bound.outcome.value, "condition": bound.runtime_condition.value},
        )
        return self._copy(bound)

    def update_task(
        self,
        task: Task,
        *,
        expected_version: int,
        lease_guard: LeaseGuard | None = None,
    ) -> Task:
        current = self.get_task(task.id)
        self._check_version(task.id, current.version, expected_version)
        self._require_session_guard(current, lease_guard)
        if current.execution.append_only_optimization != task.execution.append_only_optimization:
            raise ValueError("task append_only optimization is immutable")
        if current.session_id != task.session_id:
            raise ValueError("task session binding is immutable")
        if current.outcome is not TaskOutcome.ACTIVE and task.outcome is not current.outcome:
            raise ValueError("terminal task outcome is immutable")
        if (
            current.runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED
            and task.runtime_condition is not TaskRuntimeCondition.RECOVERY_REQUIRED
        ):
            raise RecoveryRequired(current.id)
        updated = task.model_copy(update={"version": current.version + 1})
        self.tasks[task.id] = self._copy(updated)
        session: Session | None = None
        if updated.outcome is not TaskOutcome.ACTIVE and updated.session_id is not None:
            session = self.get_session(updated.session_id)
            if session.active_task_id == updated.id:
                session = session.model_copy(
                    update={"active_task_id": None, "version": session.version + 1}
                )
        elif updated.session_id is not None:
            session = self.get_session(updated.session_id)
        if session is not None:
            self._journal(
                session,
                event_id=journal_event_id("task.updated", updated.id, updated.version),
                event_type="task.updated",
                task_id=updated.id,
                data={
                    "outcome": updated.outcome.value,
                    "condition": updated.runtime_condition.value,
                    "version": updated.version,
                },
            )
        return self._copy(updated)

    def claim_execution(
        self,
        execution: Execution,
        *,
        expected_version: int | None = None,
        now: datetime | None = None,
    ) -> Execution:
        current_time = datetime.now(UTC) if now is None else now
        self.get_session(execution.session_id)
        takeover = False
        for active in list(self.executions.values()):
            if active.session_id == execution.session_id and self._execution_is_active(active):
                if active.lease_expires_at > current_time:
                    raise LeaseConflict(execution.session_id, active.owner_id)
                takeover = True
                self._expire_execution(active, current_time)
        task = self.get_task(execution.task_id)
        self._check_version(task.id, task.version, expected_version)
        if task.session_id != execution.session_id:
            raise ValueError("execution session does not own its task")
        if task.runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED:
            raise RecoveryRequired(task.id)
        for active in list(self.executions.values()):
            if active.task_id == execution.task_id and self._execution_is_active(active):
                if active.lease_expires_at > current_time:
                    raise LeaseConflict(execution.task_id, active.owner_id)
                takeover = True
                self._expire_execution(active, current_time)
        if execution.id in self.executions:
            raise ValueError(f"execution already exists: {execution.id}")
        if any(item.lease_token == execution.lease_token for item in self.executions.values()):
            raise ValueError("execution lease token was already used")
        generation = 1 + max(
            (
                item.generation
                for item in self.executions.values()
                if item.task_id == execution.task_id
            ),
            default=0,
        )
        claimed = execution.model_copy(
            update={
                "generation": generation,
                "status": ExecutionStatus.RUNNING,
                "updated_at": current_time,
            }
        )
        self.executions[claimed.id] = self._copy(claimed)
        self.tasks[task.id] = task.model_copy(
            update={
                "runtime_condition": TaskRuntimeCondition.RUNNING,
                "version": task.version + 1,
            }
        )
        if task.session_id is not None:
            event_type = "lease.takeover" if takeover else "lease.acquired"
            self._journal(
                self.get_session(task.session_id),
                event_id=journal_event_id(event_type, claimed.id, claimed.generation),
                event_type=event_type,
                task_id=task.id,
                data={
                    "execution_id": claimed.id,
                    "scope": "execution",
                    "resource_id": claimed.task_id,
                    "owner_summary": lease_owner_summary(claimed.owner_id),
                    "generation": claimed.generation,
                    "recovery_advice": (
                        "verify prior managed commands before writes"
                        if takeover
                        else "renew before lease expiry"
                    ),
                },
            )
        return self._copy(claimed)

    def renew_execution(
        self, lease_guard: LeaseGuard, *, now: datetime, lease_expires_at: datetime
    ) -> Execution:
        execution = self._assert_guard(lease_guard, now=now)
        if lease_expires_at <= now:
            raise ValueError("renewed execution lease must expire in the future")
        renewed = execution.model_copy(
            update={
                "lease_expires_at": lease_expires_at,
                "version": execution.version + 1,
                "updated_at": now,
            }
        )
        self.executions[renewed.id] = renewed
        return self._copy(renewed)

    def release_execution(self, lease_guard: LeaseGuard, *, now: datetime) -> None:
        execution = self._assert_guard(lease_guard, now=now)
        released = execution.model_copy(
            update={
                "status": ExecutionStatus.RELEASED,
                "version": execution.version + 1,
                "updated_at": now,
            }
        )
        self.executions[released.id] = released
        task = self.tasks[released.task_id]
        if task.runtime_condition is TaskRuntimeCondition.RUNNING:
            self.tasks[task.id] = task.model_copy(
                update={
                    "runtime_condition": TaskRuntimeCondition.IDLE,
                    "version": task.version + 1,
                }
            )
        if task.session_id is not None:
            self._journal(
                self.get_session(task.session_id),
                event_id=journal_event_id("lease.released", released.id, released.version),
                event_type="lease.released",
                task_id=task.id,
                data={
                    "execution_id": released.id,
                    "scope": "execution",
                    "resource_id": released.task_id,
                    "owner_summary": lease_owner_summary(released.owner_id),
                    "generation": released.generation,
                    "recovery_advice": "safe to acquire a new execution lease",
                },
            )

    def assert_execution(self, lease_guard: LeaseGuard, *, now: datetime) -> Execution:
        return self._copy(self._assert_guard(lease_guard, now=now))

    def acquire_workspace_writer(
        self, lease: WorkspaceLease, *, lease_guard: LeaseGuard, now: datetime
    ) -> WorkspaceLease:
        existing = self.workspace_leases.get(lease.workspace_id)
        if existing is not None and existing.lease_expires_at > now:
            raise LeaseConflict(lease.workspace_id, existing.owner_id)
        execution = self._assert_guard(lease_guard, now=now)
        if (
            lease.execution_id != execution.id
            or lease.session_id != execution.session_id
            or lease.task_id != execution.task_id
            or lease.owner_id != execution.owner_id
        ):
            raise LeaseLost(lease.workspace_id)
        if existing is not None and existing.lease_token == lease.lease_token:
            raise ValueError("workspace lease token was already used")
        takeover = bool(
            existing is not None
            and (prior := self.executions.get(existing.execution_id)) is not None
            and self._execution_is_active(prior)
        )
        acquired = lease.model_copy(
            update={
                "generation": 1 if existing is None else existing.generation + 1,
                "acquired_at": now,
                "updated_at": now,
            }
        )
        self.workspace_leases[lease.workspace_id] = self._copy(acquired)
        event_type = "lease.takeover" if takeover else "lease.acquired"
        self._journal(
            self.get_session(acquired.session_id),
            event_id=journal_event_id(event_type, acquired.workspace_id, acquired.generation),
            event_type=event_type,
            task_id=acquired.task_id,
            data={
                "execution_id": acquired.execution_id,
                "scope": "workspace",
                "resource_id": acquired.workspace_id,
                "owner_summary": lease_owner_summary(acquired.owner_id),
                "generation": acquired.generation,
                "recovery_advice": (
                    "prior writer cleanup confirmed before takeover"
                    if takeover
                    else "release after managed command cleanup"
                ),
            },
        )
        return self._copy(acquired)

    def renew_workspace_writer(
        self,
        lease_guard: WorkspaceLeaseGuard,
        *,
        now: datetime,
        lease_expires_at: datetime,
    ) -> WorkspaceLease:
        existing = self._assert_workspace_guard(lease_guard, now=now)
        if lease_expires_at <= now:
            raise ValueError("renewed workspace lease must expire in the future")
        renewed = existing.model_copy(
            update={"lease_expires_at": lease_expires_at, "updated_at": now}
        )
        self.workspace_leases[renewed.workspace_id] = renewed
        return self._copy(renewed)

    def release_workspace_writer(self, lease_guard: WorkspaceLeaseGuard, *, now: datetime) -> None:
        existing = self._assert_workspace_guard(lease_guard, now=now)
        self.workspace_leases[existing.workspace_id] = existing.model_copy(
            update={"lease_expires_at": now, "updated_at": now}
        )
        self._journal(
            self.get_session(existing.session_id),
            event_id=journal_event_id(
                "lease.released",
                existing.workspace_id,
                f"{existing.generation}:{existing.execution_id}",
            ),
            event_type="lease.released",
            task_id=existing.task_id,
            data={
                "execution_id": existing.execution_id,
                "scope": "workspace",
                "resource_id": existing.workspace_id,
                "owner_summary": lease_owner_summary(existing.owner_id),
                "generation": existing.generation,
                "recovery_advice": "safe to acquire a new workspace writer",
            },
        )

    def assert_workspace_writer(
        self, lease_guard: WorkspaceLeaseGuard, *, now: datetime
    ) -> WorkspaceLease:
        return self._copy(self._assert_workspace_guard(lease_guard, now=now))

    def recovery_commands_for_task(
        self, task_id: str, *, now: datetime
    ) -> list[ManagedCommandIdentity]:
        return [
            self._copy(command)
            for command in self.managed_commands.values()
            if (execution := self.executions.get(command.execution_id)) is not None
            and execution.task_id == task_id
            and execution.lease_expires_at <= now
            and command.status
            in {ManagedCommandStatus.RUNNING, ManagedCommandStatus.CLEANUP_FAILED}
        ]

    def recovery_commands_for_workspace(
        self, workspace_id: str, *, now: datetime
    ) -> list[ManagedCommandIdentity]:
        lease = self.workspace_leases.get(workspace_id)
        if lease is None or lease.lease_expires_at > now:
            return []
        return [
            self._copy(command)
            for command in self.managed_commands.values()
            if command.execution_id == lease.execution_id
            and command.status
            in {ManagedCommandStatus.RUNNING, ManagedCommandStatus.CLEANUP_FAILED}
        ]

    def finish_managed_command(self, identity: ManagedCommandIdentity) -> ManagedCommandIdentity:
        current = self.managed_commands.get(identity.id)
        if current is not None and current.status in {
            ManagedCommandStatus.EXITED,
            ManagedCommandStatus.TERMINATED,
        }:
            return self._copy(current)
        self.managed_commands[identity.id] = self._copy(identity)
        return self._copy(identity)

    def mark_command_recovery_required(
        self, identity: ManagedCommandIdentity, *, cleanup_info: str
    ) -> str:
        del cleanup_info
        execution = self.executions[identity.execution_id]
        self.executions[execution.id] = execution.model_copy(
            update={"status": ExecutionStatus.RECOVERY_REQUIRED, "version": execution.version + 1}
        )
        task = self.tasks[execution.task_id]
        self.tasks[task.id] = task.model_copy(
            update={
                "runtime_condition": TaskRuntimeCondition.RECOVERY_REQUIRED,
                "version": task.version + 1,
            }
        )
        return task.id

    def _assert_workspace_guard(
        self, lease_guard: WorkspaceLeaseGuard, *, now: datetime
    ) -> WorkspaceLease:
        existing = self.workspace_leases.get(lease_guard.workspace_id)
        if (
            existing is None
            or existing.execution_id != lease_guard.execution_id
            or existing.lease_token != lease_guard.token
            or existing.generation != lease_guard.generation
            or existing.owner_id != lease_guard.owner_id
            or existing.lease_expires_at <= now
        ):
            raise LeaseLost(lease_guard.workspace_id)
        return existing

    @staticmethod
    def _execution_is_active(execution: Execution) -> bool:
        return execution.status in {
            ExecutionStatus.CLAIMED,
            ExecutionStatus.RUNNING,
            ExecutionStatus.WAITING_FOR_APPROVAL,
            ExecutionStatus.PAUSED,
        }

    def _expire_execution(self, execution: Execution, now: datetime) -> None:
        if not self._execution_is_active(execution):
            return
        self.executions[execution.id] = execution.model_copy(
            update={
                "status": ExecutionStatus.RELEASED,
                "version": execution.version + 1,
                "updated_at": now,
            }
        )

    def _assert_guard(self, guard: LeaseGuard, *, now: datetime | None = None) -> Execution:
        current_time = datetime.now(UTC) if now is None else now
        execution = self.executions.get(guard.execution_id)
        if (
            execution is None
            or execution.task_id != guard.task_id
            or execution.lease_token != guard.token
            or execution.generation != guard.generation
            or execution.owner_id != guard.owner_id
            or execution.lease_expires_at <= current_time
            or execution.status
            not in {
                ExecutionStatus.CLAIMED,
                ExecutionStatus.RUNNING,
                ExecutionStatus.WAITING_FOR_APPROVAL,
                ExecutionStatus.PAUSED,
            }
        ):
            raise LeaseLost(guard.task_id)
        return execution

    def prepare_effects(
        self,
        effects: Sequence[Effect],
        *,
        expected_version: int,
        lease_guard: LeaseGuard | None = None,
    ) -> list[Effect]:
        if not effects:
            return []
        task_ids = {effect.task_id for effect in effects}
        if len(task_ids) != 1:
            raise ValueError("one prepare batch must belong to one task")
        task_id = next(iter(task_ids))
        task = self.get_task(task_id)
        self._require_session_guard(task, lease_guard)
        result: list[Effect] = []
        new_effects: list[Effect] = []
        for effect in effects:
            existing = self.effects.get(effect.id)
            if existing is not None:
                existing.assert_identity_compatible(effect)
                result.append(self._copy(existing))
                continue
            for candidate in self.effects.values():
                if candidate.identity_key() != effect.identity_key():
                    continue
                candidate.assert_identity_compatible(effect)
                result.append(self._copy(candidate))
                break
            else:
                pending = next(
                    (
                        candidate
                        for candidate in new_effects
                        if candidate.id == effect.id
                        or candidate.identity_key() == effect.identity_key()
                    ),
                    None,
                )
                if pending is not None:
                    pending.assert_identity_compatible(effect)
                    result.append(self._copy(pending))
                    continue
                new_effects.append(effect)
                result.append(self._copy(effect))
        if new_effects:
            self._check_version(task_id, task.version, expected_version)
            for effect in new_effects:
                self.effects[effect.id] = self._copy(effect)
            if effect.policy_evaluation is not None:
                self._journal_policy(
                    effect, effect.policy_evaluation.model_dump(mode="json"), "prepared"
                )
                if effect.policy_evaluation is not None:
                    self._journal_policy(
                        effect, effect.policy_evaluation.model_dump(mode="json"), "prepared"
                    )
            if task.session_id is not None:
                effect_ids = [effect.id for effect in new_effects]
                self._journal(
                    self.get_session(task.session_id),
                    event_id=journal_event_id("effects.prepared", task.id, ",".join(effect_ids)),
                    event_type="effects.prepared",
                    task_id=task.id,
                    data={"effect_ids": effect_ids},
                )
        return result

    def prepare_effect_batch(
        self,
        step: AgentStep,
        effects: Sequence[Effect],
        *,
        expected_version: int,
        lease_guard: LeaseGuard | None = None,
        provider_request_id: str | None = None,
    ) -> list[Effect]:
        if step.model_response is None:
            raise ValueError("an Effect batch requires a persisted model response")
        if step.effect_ids != [effect.id for effect in effects]:
            raise ValueError("step Effect IDs must preserve batch order")
        if any(
            effect.task_id != step.task_id
            or effect.step_id != step.id
            or effect.batch_position != position
            for position, effect in enumerate(effects)
        ):
            raise ValueError("Effect batch does not match its Step")
        safe_step = AgentStep.model_validate(
            self._safe_provider_value(step.model_dump(mode="json"))
        )
        task = self.get_task(step.task_id)
        self._require_session_guard(task, lease_guard)
        self._check_version(task.id, task.version, expected_version)
        provider_request = None
        if provider_request_id is not None:
            self._assert_provider_guard(step.task_id, lease_guard)
            provider_request = self.get_provider_request(provider_request_id)
            if provider_request.task_id != step.task_id:
                raise ProviderRequestConflict(
                    provider_request_id, "provider request belongs to another task"
                )
            if provider_request.status not in {
                ProviderRequestStatus.RESPONSE_READY,
                ProviderRequestStatus.COMPLETED,
            }:
                raise ProviderRequestConflict(
                    provider_request_id, "provider response is not complete"
                )
            if (
                provider_request.response is None
                or self._safe_provider_value(provider_request.response.model_dump(mode="json"))
                != safe_step.model_response
            ):
                raise ProviderRequestConflict(
                    provider_request_id, "Step response differs from provider response"
                )
        key = (step.task_id, step.index)
        existing = self.steps.get(key)
        if existing is not None and (
            existing.id != safe_step.id
            or (
                existing.model_response is not None
                and existing.model_response != safe_step.model_response
            )
            or (existing.effect_ids and existing.effect_ids != safe_step.effect_ids)
        ):
            raise EffectIdentityConflict(safe_step.id, (step.task_id, step.id, step.index))
        prepared: list[Effect] = []
        new_effects: list[Effect] = []
        for effect in effects:
            existing_effect = next(
                (
                    candidate
                    for candidate in self.effects.values()
                    if candidate.id == effect.id
                    or candidate.identity_key() == effect.identity_key()
                ),
                None,
            )
            if existing_effect is not None:
                existing_effect.assert_identity_compatible(effect)
                prepared.append(self._copy(existing_effect))
            else:
                new_effects.append(effect)
                prepared.append(self._copy(effect))
        self.steps[key] = self._copy(safe_step)
        for effect in new_effects:
            self.effects[effect.id] = self._copy(effect)
            if effect.policy_evaluation is not None:
                self._journal_policy(
                    effect, effect.policy_evaluation.model_dump(mode="json"), "prepared"
                )
        if (
            provider_request is not None
            and provider_request.status is ProviderRequestStatus.RESPONSE_READY
        ):
            assert provider_request_id is not None
            self.provider_requests[provider_request_id] = provider_request.model_copy(
                update={"status": ProviderRequestStatus.COMPLETED, "updated_at": datetime.now(UTC)}
            )
        if new_effects and task.session_id is not None:
            effect_ids = [effect.id for effect in new_effects]
            self._journal(
                self.get_session(task.session_id),
                event_id=journal_event_id("effects.prepared", task.id, ",".join(effect_ids)),
                event_type="effects.prepared",
                task_id=task.id,
                data={"effect_ids": effect_ids, "step_id": step.id},
            )
        return prepared

    def begin_provider_request(
        self, record: ProviderRequestRecord, *, lease_guard: LeaseGuard
    ) -> ProviderRequestRecord:
        self._assert_provider_guard(record.task_id, lease_guard)
        existing = self.provider_requests.get(record.request_id)
        if existing is not None:
            if _provider_request_identity(existing) != _provider_request_identity(record):
                raise ProviderRequestConflict(record.request_id)
            if existing.status is ProviderRequestStatus.INVALIDATED:
                raise ProviderRequestConflict(record.request_id, "provider request was invalidated")
            return self._copy(existing)
        for current in self.provider_requests.values():
            if current.logical_key == record.logical_key:
                if _provider_request_identity(current) == _provider_request_identity(record):
                    if current.status is ProviderRequestStatus.INVALIDATED:
                        raise ProviderRequestConflict(
                            record.request_id, "provider request was invalidated"
                        )
                    return self._copy(current)
                raise ProviderRequestConflict(
                    record.request_id, "logical provider request already exists"
                )
        task = self.get_task(record.task_id)
        self.provider_requests[record.request_id] = self._copy(record)
        if task.session_id is not None:
            self._journal(
                self.get_session(task.session_id),
                event_id=journal_event_id("provider.request.started", record.request_id, 1),
                event_type="provider.request.started",
                task_id=task.id,
                data={"request_id": record.request_id, "purpose": record.purpose.value},
            )
        return self._copy(record)

    def begin_provider_attempt(
        self, record: ProviderAttemptRecord, *, lease_guard: LeaseGuard
    ) -> ProviderAttemptRecord:
        self._assert_provider_guard(lease_guard.task_id, lease_guard)
        request = self.get_provider_request(record.request_id)
        if (
            request.task_id != lease_guard.task_id
            or record.execution_id != lease_guard.execution_id
        ):
            raise LeaseLost(lease_guard.task_id)
        if request.status is not ProviderRequestStatus.PENDING:
            raise ProviderRequestConflict(
                request.request_id, "provider request is not awaiting an attempt"
            )
        existing = self.provider_attempts.get(record.attempt_id)
        if existing is not None:
            if existing == record:
                return self._copy(existing)
            raise ProviderRequestConflict(request.request_id, "provider attempt identity conflict")
        prior = self.list_provider_attempts(record.request_id)
        if record.ordinal != len(prior) + 1:
            raise ProviderRequestConflict(
                request.request_id, "provider attempt ordinal is not sequential"
            )
        self._interrupt_open_provider_attempts(record.request_id, datetime.now(UTC))
        self.provider_attempts[record.attempt_id] = self._copy(record)
        self._journal_provider_attempt(request.task_id, record, "provider.attempt.started")
        return self._copy(record)

    def finish_provider_attempt(
        self,
        attempt_id: str,
        outcome: ProviderAttemptOutcome,
        *,
        lease_guard: LeaseGuard,
    ) -> ProviderAttemptRecord:
        attempt = self.provider_attempts.get(attempt_id)
        if attempt is None:
            raise KeyError(f"provider attempt not found: {attempt_id}")
        self._assert_provider_guard(lease_guard.task_id, lease_guard)
        if (
            attempt.execution_id != lease_guard.execution_id
            or attempt.request_id not in self.provider_requests
        ):
            raise LeaseLost(lease_guard.task_id)
        if attempt.status is not ProviderAttemptStatus.RUNNING:
            if attempt.status is outcome.status:
                return self._copy(attempt)
            raise ProviderRequestConflict(
                attempt.request_id, "provider attempt already has another outcome"
            )
        finished = attempt.model_copy(
            update={
                "status": outcome.status,
                "finished_at": datetime.now(UTC),
                "error_kind": outcome.error_kind,
                "safe_message": self._safe_provider_value(outcome.safe_message),
                "usage_status": outcome.usage_status,
                "usage": outcome.usage,
                "request_sent": outcome.request_sent,
            }
        )
        self.provider_attempts[attempt_id] = finished
        self._journal_provider_attempt(
            self.provider_requests[attempt.request_id].task_id,
            finished,
            f"provider.attempt.{finished.status.value}",
        )
        return self._copy(finished)

    def commit_provider_response(
        self,
        request_id: str,
        response: ModelResponse,
        *,
        lease_guard: LeaseGuard,
    ) -> ProviderRequestRecord:
        from patchloop.providers.continuation import ContinuationCodec

        request = self.get_provider_request(request_id)
        self._assert_provider_guard(request.task_id, lease_guard)
        if request.status in {
            ProviderRequestStatus.RESPONSE_READY,
            ProviderRequestStatus.COMPLETED,
        }:
            safe_response = ContinuationCodec().to_storage(response)
            if request.response == safe_response:
                return request
            raise ProviderRequestConflict(
                request_id, "provider response conflicts with committed response"
            )
        attempt = next(
            (
                item
                for item in reversed(self.list_provider_attempts(request_id))
                if item.status is ProviderAttemptStatus.RUNNING
                and item.execution_id == lease_guard.execution_id
            ),
            None,
        )
        if attempt is None:
            raise ProviderRequestConflict(request_id, "complete response has no active attempt")
        safe_response = ContinuationCodec().to_storage(response)
        digest = ContinuationCodec.response_digest(safe_response)
        status = (
            ProviderRequestStatus.COMPLETED
            if request.purpose is ProviderRequestPurpose.EPOCH_COMPRESSION
            else ProviderRequestStatus.RESPONSE_READY
        )
        updated = request.model_copy(
            update={
                "status": status,
                "response": safe_response,
                "response_sha256": digest,
                "usage": safe_response.usage,
                "updated_at": datetime.now(UTC),
            }
        )
        self.provider_requests[request_id] = updated
        succeeded = attempt.model_copy(
            update={
                "status": ProviderAttemptStatus.SUCCEEDED,
                "finished_at": datetime.now(UTC),
                "usage_status": _provider_usage_status(safe_response.usage),
                "usage": safe_response.usage,
                "request_sent": True,
            }
        )
        self.provider_attempts[attempt.attempt_id] = succeeded
        self._journal_provider_attempt(updated.task_id, succeeded, "provider.attempt.succeeded")
        self._journal_provider_request(updated.task_id, updated)
        return self._copy(updated)

    def get_provider_request(self, request_id: str) -> ProviderRequestRecord:
        from patchloop.providers.continuation import ContinuationCodec

        request = self.provider_requests.get(request_id)
        if request is None:
            raise KeyError(f"provider request not found: {request_id}")
        if (
            request.response is not None
            and ContinuationCodec.response_digest(request.response) != request.response_sha256
        ):
            raise ProviderRequestConflict(
                request_id, "stored provider response failed integrity check"
            )
        return self._copy(request)

    def list_provider_attempts(self, request_id: str) -> list[ProviderAttemptRecord]:
        return [
            self._copy(item)
            for item in sorted(
                (
                    attempt
                    for attempt in self.provider_attempts.values()
                    if attempt.request_id == request_id
                ),
                key=lambda attempt: attempt.ordinal,
            )
        ]

    def invalidate_provider_request(
        self, request_id: str, *, lease_guard: LeaseGuard
    ) -> ProviderRequestRecord:
        request = self.get_provider_request(request_id)
        self._assert_provider_guard(request.task_id, lease_guard)
        if request.status is ProviderRequestStatus.COMPLETED:
            raise ProviderRequestConflict(
                request_id, "completed provider request cannot be invalidated"
            )
        self._interrupt_open_provider_attempts(request_id, datetime.now(UTC))
        invalidated = request.model_copy(
            update={
                "status": ProviderRequestStatus.INVALIDATED,
                "response": None,
                "response_sha256": None,
                "usage": None,
                "updated_at": datetime.now(UTC),
            }
        )
        self.provider_requests[request_id] = invalidated
        task = self.get_task(request.task_id)
        if task.session_id is not None:
            self._journal(
                self.get_session(task.session_id),
                event_id=journal_event_id(
                    "provider.request.invalidated", request_id, request.input_revision
                ),
                event_type="provider.request.invalidated",
                task_id=task.id,
                data={"request_id": request_id, "input_revision": request.input_revision},
            )
        return self._copy(invalidated)

    def _assert_provider_guard(self, task_id: str, lease_guard: LeaseGuard | None) -> Execution:
        if lease_guard is None or lease_guard.task_id != task_id:
            raise LeaseLost(task_id)
        return self._assert_guard(lease_guard)

    def _interrupt_open_provider_attempts(self, request_id: str, now: datetime) -> None:
        request = self.provider_requests[request_id]
        for attempt in self.list_provider_attempts(request_id):
            if attempt.status is not ProviderAttemptStatus.RUNNING:
                continue
            interrupted = attempt.model_copy(
                update={
                    "status": ProviderAttemptStatus.INTERRUPTED,
                    "finished_at": now,
                    "usage_status": ProviderUsageStatus.UNKNOWN,
                }
            )
            self.provider_attempts[attempt.attempt_id] = interrupted
            self._journal_provider_attempt(
                request.task_id, interrupted, "provider.attempt.interrupted"
            )

    def _journal_provider_attempt(
        self, task_id: str, attempt: ProviderAttemptRecord, event_type: str
    ) -> None:
        task = self.get_task(task_id)
        if task.session_id is None:
            return
        self._journal(
            self.get_session(task.session_id),
            event_id=journal_event_id(event_type, attempt.attempt_id, attempt.ordinal),
            event_type=event_type,
            task_id=task.id,
            data={
                "request_id": attempt.request_id,
                "attempt_id": attempt.attempt_id,
                "ordinal": attempt.ordinal,
                "status": attempt.status.value,
                "usage_status": attempt.usage_status.value,
            },
        )

    def _journal_provider_request(self, task_id: str, request: ProviderRequestRecord) -> None:
        task = self.get_task(task_id)
        if task.session_id is None:
            return
        self._journal(
            self.get_session(task.session_id),
            event_id=journal_event_id(
                "provider.response.saved", request.request_id, request.updated_at.isoformat()
            ),
            event_type="provider.response.saved",
            task_id=task.id,
            data={"request_id": request.request_id, "status": request.status.value},
        )

    @staticmethod
    def _safe_provider_value(value: object) -> object:
        from patchloop.providers.continuation import ContinuationCodec

        return ContinuationCodec().to_storage(value)

    @_policy_atomic
    def decide_approval(
        self, approval: Approval, *, expected_version: int | None = None
    ) -> Approval:
        self.get_effect(approval.effect_id)
        existing = next(
            (
                candidate
                for candidate in self.approvals.values()
                if candidate.effect_id == approval.effect_id
            ),
            None,
        )
        if existing is None:
            self.approvals[approval.id] = self._copy(approval)
            self._journal_approval(approval, "approval.requested")
            return self._copy(approval)
        if existing == approval:
            return self._copy(existing)
        self._check_version(approval.id, existing.version, expected_version)
        if existing.status is not ApprovalStatus.PENDING or approval.status not in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.DENIED,
            ApprovalStatus.EXPIRED,
        }:
            raise ApprovalConflict(approval.id, existing.status.value, approval.status.value)
        if approval.version != existing.version + 1:
            raise StaleVersion(approval.id, existing.version + 1, approval.version)
        self.approvals[approval.id] = self._copy(approval)
        self._journal_approval(approval, "approval.decided")
        return self._copy(approval)

    @_policy_atomic
    def replace_effect_approval(
        self,
        effect_id: str,
        replacement: Effect,
        approval: Approval,
        *,
        expected_version: int,
        lease_guard: LeaseGuard,
    ) -> tuple[Effect, Approval, Task]:
        from patchloop.execution.approvals import validate_replacement

        self._assert_guard(lease_guard)
        original = self.get_effect(effect_id)
        self._check_version(original.id, original.version, expected_version)
        validate_replacement(original, replacement, approval)
        if any(
            item.id == replacement.id or item.identity_key() == replacement.identity_key()
            for item in self.effects.values()
        ):
            raise EffectIdentityConflict(replacement.id, replacement.identity_key())
        names = ("effects", "approvals", "grants", "tasks", "sessions", "executions", "events")
        snapshot = {name: deepcopy(getattr(self, name)) for name in names}
        try:
            self.effects[replacement.id] = self._copy(replacement)
            if replacement.policy_evaluation is not None:
                self._journal_policy(
                    replacement, replacement.policy_evaluation.model_dump(mode="json"), "prepared"
                )
            return self.request_effect_approval(
                replacement.id,
                approval,
                expected_version=replacement.version,
                lease_guard=lease_guard,
            )
        except Exception:
            for name, value in snapshot.items():
                setattr(self, name, value)
            raise

    @_policy_atomic
    def request_effect_approval(
        self,
        effect_id: str,
        approval: Approval,
        *,
        expected_version: int,
        lease_guard: LeaseGuard,
    ) -> tuple[Effect, Approval, Task]:
        execution = self._assert_guard(lease_guard)
        current = self.get_effect(effect_id)
        if current.task_id != execution.task_id:
            raise LeaseLost(current.task_id)
        self._check_version(effect_id, current.version, expected_version)
        if approval.effect_id != effect_id:
            raise ValueError("approval does not belong to the Effect")
        if current.status is EffectStatus.WAITING_FOR_APPROVAL:
            existing = self.approvals.get(current.approval_id or "")
            if existing is not None and existing.same_request(approval):
                task = self.get_task(current.task_id)
                waiting_execution = execution.model_copy(
                    update={
                        "status": ExecutionStatus.WAITING_FOR_APPROVAL,
                        "version": execution.version + 1,
                    }
                )
                waiting_task = task.model_copy(
                    update={
                        "runtime_condition": TaskRuntimeCondition.WAITING_FOR_APPROVAL,
                        "version": task.version + 1,
                    }
                )
                self.executions[execution.id] = self._copy(waiting_execution)
                self.tasks[task.id] = self._copy(waiting_task)
                return current, self._copy(existing), self._copy(waiting_task)
            raise ApprovalConflict(approval.id, "pending", "pending")
        if current.status is not EffectStatus.PREPARED:
            raise ValueError(f"Effect cannot request approval from {current.status}")
        if approval.status is not ApprovalStatus.PENDING:
            raise ValueError("new approval request must be pending")
        if approval.id in self.approvals or any(
            candidate.effect_id == effect_id for candidate in self.approvals.values()
        ):
            raise ApprovalConflict(approval.id, "pending", "pending")
        task = self.get_task(current.task_id)
        if task.runtime_condition is not TaskRuntimeCondition.RUNNING:
            raise ValueError("approval can only pause a running Task")
        if approval.supersedes_approval_id is not None:
            previous = self.approvals[approval.supersedes_approval_id]
            original = self.get_effect(previous.effect_id)
            expired, cancelled = supersede_approval(previous, original, current)
            self.approvals[expired.id] = self._copy(expired)
            self.effects[cancelled.id] = self._copy(cancelled)
            if previous.grant_id is not None:
                grant = self.grants[previous.grant_id]
                if grant.status == "active":
                    revoked = grant.model_copy(
                        update={
                            "status": "revoked",
                            "version": grant.version + 1,
                            "revoked_at": datetime.now(UTC),
                            "revoked_by": "superseded",
                        }
                    )
                    self.grants[grant.id] = self._copy(revoked)
                    self._journal_grant(revoked, "approval.grant_revoked")
        waiting_effect = current.model_copy(
            update={
                "status": EffectStatus.WAITING_FOR_APPROVAL,
                "approval_id": approval.id,
                "version": current.version + 1,
            }
        )
        waiting_execution = execution.model_copy(
            update={
                "status": ExecutionStatus.WAITING_FOR_APPROVAL,
                "version": execution.version + 1,
            }
        )
        waiting_task = task.model_copy(
            update={
                "runtime_condition": TaskRuntimeCondition.WAITING_FOR_APPROVAL,
                "version": task.version + 1,
            }
        )
        self.effects[effect_id] = self._copy(waiting_effect)
        self.approvals[approval.id] = self._copy(approval)
        self.executions[execution.id] = self._copy(waiting_execution)
        self.tasks[task.id] = self._copy(waiting_task)
        self._journal_approval(approval, "approval.requested")
        if approval.supersedes_approval_id is not None:
            self._journal_approval(approval, "approval.superseded")
        return (
            self._copy(waiting_effect),
            self._copy(approval),
            self._copy(waiting_task),
        )

    @_policy_atomic
    def resolve_effect_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        source: str,
        expected_version: int,
        workspace_ref: str,
        policy_version: str,
        config_version: str,
        scope_kind: ApprovalScopeKind = ApprovalScopeKind.ONCE,
        expires_at: datetime | None = None,
        reason: str | None = None,
    ) -> ApprovalResolution:
        try:
            current = self._copy(self.approvals[approval_id])
        except KeyError as exc:
            raise KeyError(f"approval not found: {approval_id}") from exc
        effect = self.get_effect(current.effect_id)
        task = self.get_task(effect.task_id)
        target = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED
        if current.status is not ApprovalStatus.PENDING:
            if current.scope_kind != scope_kind or current.expires_at != expires_at:
                raise ApprovalConflict(current.id, "scope_mismatch", target.value)
            if (
                approved
                and current.status is ApprovalStatus.CONSUMED
                and current.decision_source == source
            ):
                return ApprovalResolution(
                    current,
                    effect,
                    task,
                    None if current.grant_id is None else self.get_grant(current.grant_id),
                )
            if current.status is target and current.decision_source == source:
                return ApprovalResolution(
                    current,
                    effect,
                    task,
                    None if current.grant_id is None else self.get_grant(current.grant_id),
                )
            raise ApprovalConflict(current.id, current.status.value, target.value)
        self._check_version(current.id, current.version, expected_version)
        actual_config: str = (
            config_version
            if task.session_id is None
            else self.get_session(task.session_id).config_version
        ) or "1"
        matches = (
            workspace_ref == task.repository
            and config_version == actual_config
            and current.matches_execution_conditions(
                effect,
                workspace_ref=workspace_ref,
                policy_version=policy_version,
                config_version=config_version,
            )
        )
        if not matches:
            decided = current.model_copy(
                update={
                    "status": ApprovalStatus.EXPIRED,
                    "decision_source": source,
                    "decided_at": datetime.now(UTC),
                    "version": current.version + 1,
                }
            )
            released_effect = effect.model_copy(
                update={
                    "status": EffectStatus.PREPARED,
                    "approval_id": None,
                    "version": effect.version + 1,
                }
            )
            released_task = task
            if task.runtime_condition is TaskRuntimeCondition.WAITING_FOR_APPROVAL:
                released_task = task.model_copy(
                    update={
                        "runtime_condition": TaskRuntimeCondition.IDLE,
                        "version": task.version + 1,
                    }
                )
            self.approvals[current.id] = self._copy(decided)
            self.effects[effect.id] = self._copy(released_effect)
            self.tasks[task.id] = self._copy(released_task)
            self._journal_approval(decided, "approval.expired")
            return ApprovalResolution(
                self._copy(decided), self._copy(released_effect), self._copy(released_task)
            )
        scoped, grant = scope_approval(
            current,
            effect,
            task,
            scope_kind=scope_kind if approved else ApprovalScopeKind.ONCE,
            expires_at=expires_at,
            reason=reason,
        )
        decided = scoped.decide(approved, source)
        next_effect_status = EffectStatus.PREPARED if approved else EffectStatus.DENIED
        resolved_effect = effect.model_copy(
            update={
                "status": next_effect_status,
                "version": effect.version + 1,
            }
        )
        resolved_task = task
        if task.runtime_condition is TaskRuntimeCondition.WAITING_FOR_APPROVAL:
            resolved_task = task.model_copy(
                update={
                    "runtime_condition": TaskRuntimeCondition.IDLE,
                    "version": task.version + 1,
                }
            )
        self.approvals[current.id] = self._copy(decided)
        self.effects[effect.id] = self._copy(resolved_effect)
        self.tasks[task.id] = self._copy(resolved_task)
        self._journal_approval(decided, "approval.decided")
        if grant is not None:
            self.grants[grant.id] = self._copy(grant)
            self._journal_grant(grant, "approval.grant_created")
        return ApprovalResolution(
            self._copy(decided),
            self._copy(resolved_effect),
            self._copy(resolved_task),
            None if grant is None else self._copy(grant),
        )

    @_policy_atomic
    def put_policy_rule(self, rule: PolicyRule) -> PolicyRule:
        self.policy_rules[rule.id] = self._copy(rule)
        return self._copy(rule)

    @_policy_atomic
    def list_policy_rules(self, workspace_ref: str) -> list[PolicyRule]:
        return [
            self._copy(rule)
            for rule in sorted(self.policy_rules.values(), key=lambda r: r.id)
            if rule.workspace_ref == workspace_ref
        ]

    def _journal_policy(self, effect: Effect, data: dict[str, object], phase: str) -> None:
        task = self.get_task(effect.task_id)
        if task.session_id is not None:
            self._journal(
                self.get_session(task.session_id),
                event_id=journal_event_id("policy.evaluated", effect.id, phase),
                event_type="policy.evaluated",
                task_id=task.id,
                data={"effect_id": effect.id, "phase": phase, "evaluation": data},
            )

    def _journal_grant(self, grant: ApprovalGrant, event_type: str) -> None:
        approval = self.approvals[grant.source_approval_id]
        effect = self.get_effect(approval.effect_id)
        task = self.get_task(effect.task_id)
        if task.session_id is not None:
            self._journal(
                self.get_session(task.session_id),
                event_id=journal_event_id(event_type, grant.id, grant.version),
                event_type=event_type,
                task_id=task.id,
                data={"grant": grant.model_dump(mode="json")},
            )

    @_policy_atomic
    def get_grant(self, grant_id: str) -> ApprovalGrant:
        grant = self.grants[grant_id]
        expired = grant.expire()
        if expired != grant:
            self.grants[grant_id] = self._copy(expired)
            self._journal_grant(expired, "approval.expired")
        return self._copy(expired)

    @_policy_atomic
    def list_grants(self, scope_id: str) -> list[ApprovalGrant]:
        return [
            self.get_grant(grant.id)
            for grant in sorted(self.grants.values(), key=lambda g: g.id)
            if scope_id in {grant.workspace_ref, grant.session_id}
        ]

    @_policy_atomic
    def consume_grant(
        self,
        grant_id: str,
        descriptor: ActionDescriptor,
        *,
        expected_version: int,
        policy_version: str,
        config_version: str,
        configured_rules: tuple[PolicyRule, ...] = (),
    ) -> ApprovalGrant:
        grant = self.get_grant(grant_id)
        self._check_version(grant.id, grant.version, expected_version)
        validate_grant_consumption(
            grant,
            descriptor,
            rules=(*configured_rules, *self.list_policy_rules(descriptor.workspace_ref)),
            policy_version=policy_version,
            config_version=config_version,
        )
        consumed = grant.consume(
            descriptor, policy_version=policy_version, config_version=config_version
        )
        self.grants[grant_id] = self._copy(consumed)
        self._journal_grant(consumed, "approval.grant_consumed")
        return self._copy(consumed)

    @_policy_atomic
    def revoke_grant(
        self, grant_id: str, *, source: str, reason: str | None = None, expected_version: int
    ) -> ApprovalGrant:
        grant = self.get_grant(grant_id)
        self._check_version(grant.id, grant.version, expected_version)
        if grant.status != "active":
            raise ApprovalConflict(grant.id, grant.status, "revoked")
        revoked = grant.model_copy(
            update={
                "status": "revoked",
                "revoked_at": datetime.now(UTC),
                "revoked_by": SecretRedactor().redact_text(source),
                "revoked_reason": None if reason is None else SecretRedactor().redact_text(reason),
                "version": grant.version + 1,
            }
        )
        self.grants[grant_id] = self._copy(revoked)
        self._journal_grant(revoked, "approval.grant_revoked")
        return self._copy(revoked)

    @_policy_atomic
    def claim_effect(
        self,
        effect_id: str,
        *,
        expected_version: int,
        lease_guard: LeaseGuard,
        effect_fingerprint: str | None = None,
        workspace_ref: str | None = None,
        policy_version: str | None = None,
        config_version: str | None = None,
        policy_decision: str | None = None,
        configured_rules: tuple[PolicyRule, ...] = (),
        current_evaluation: PolicyEvaluation | None = None,
        expected_input_sequence: int | None = None,
    ) -> Effect:
        observed = self.get_effect(effect_id)
        if observed.approval_id is not None:
            self.get_approval(observed.approval_id)
        self.list_grants(self.get_task(observed.task_id).repository)
        self._assert_guard(lease_guard)
        current = self._copy(self.effects[effect_id])
        self._check_version(effect_id, current.version, expected_version)
        if current.status is not EffectStatus.PREPARED:
            raise LeaseConflict(effect_id)
        task = self.get_task(current.task_id)
        pending_control = self.get_pending_control(task.id)
        if pending_control is not None:
            raise ControlRequested(task.id, pending_control.id)
        if expected_input_sequence is not None and task.session_id is not None:
            turns = self.turns.get(task.session_id, [])
            actual_input_sequence = max((turn.sequence for turn in turns), default=0)
            if actual_input_sequence != expected_input_sequence:
                raise InputRevisionConflict(
                    task.session_id,
                    expected_input_sequence,
                    actual_input_sequence,
                )
        actual_workspace = task.repository
        actual_config: str = (
            config_version
            if task.session_id is None
            else self.get_session(task.session_id).config_version
        ) or "1"
        if effect_fingerprint is not None and current.content_fingerprint() != effect_fingerprint:
            raise EffectIdentityConflict(current.id, current.identity_key())
        if workspace_ref is not None and workspace_ref != actual_workspace:
            raise LeaseConflict(effect_id)
        if config_version is not None and config_version != actual_config:
            raise LeaseConflict(effect_id)
        persisted_decision = str(current.policy_result.get("decision", "allow"))
        if current.preparation_error is not None or persisted_decision == "deny":
            raise ValueError(f"Effect is not executable: {effect_id}")
        if policy_decision is not None and policy_decision != persisted_decision:
            raise ValueError(f"Effect policy changed before execution: {effect_id}")
        rules = tuple(self.list_policy_rules(actual_workspace))
        grants = tuple(self.list_grants(actual_workspace))
        policy_effect = current
        if current.action_descriptor is None and current_evaluation is not None:
            policy_effect = current.model_copy(
                update={"action_descriptor": current_evaluation.descriptor}
            )
        claim_evaluation = evaluate_claim_policy(
            policy_effect,
            task,
            rules=(*configured_rules, *rules),
            grants=grants if current.action_descriptor is not None else (),
            policy_version=policy_version or "",
            config_version=config_version or actual_config,
        )
        matched_grant = next(
            (
                grant
                for grant in grants
                if claim_evaluation is not None and grant.id == claim_evaluation.matched_grant_id
            ),
            None,
        )
        consumed_grant = None
        consumed_approval = None
        if current.approval_id is not None:
            approval = self._copy(self.approvals[current.approval_id])
            if approval.grant_id is not None:
                bound_grant = next(
                    (grant for grant in grants if grant.id == approval.grant_id), None
                )
                if (
                    bound_grant is None
                    or current.action_descriptor is None
                    or not bound_grant.matches(
                        current.action_descriptor,
                        policy_version or approval.policy_version,
                        config_version or actual_config,
                    )
                ):
                    raise ApprovalConflict(approval.id, "grant_invalid", "consumed")
                matched_grant = bound_grant
            consumed_approval = approval.consume(
                current,
                workspace_ref=workspace_ref or actual_workspace,
                policy_version=policy_version or approval.policy_version,
                config_version=config_version or actual_config,
            )
        elif persisted_decision == "require_approval" and matched_grant is None:
            raise ApprovalConflict(effect_id, "missing", "consumed")
        if matched_grant is not None and current.action_descriptor is not None:
            consumed_grant = matched_grant.consume(
                current.action_descriptor,
                policy_version=policy_version or "",
                config_version=config_version or actual_config,
            )
        claimed = current.model_copy(
            update={
                "status": EffectStatus.EXECUTING,
                "action_descriptor": policy_effect.action_descriptor,
                "policy_evaluation": current.policy_evaluation or current_evaluation,
                "approval_id": None,
                "approval_consumed": consumed_approval is not None or consumed_grant is not None,
                "consumed_grant_id": None if consumed_grant is None else consumed_grant.id,
                "version": current.version + 1,
            }
        )
        self.effects[effect_id] = self._copy(claimed)
        if consumed_grant is not None:
            self.grants[consumed_grant.id] = self._copy(consumed_grant)
            self._journal_grant(consumed_grant, "approval.grant_consumed")
        if consumed_approval is not None:
            self.approvals[consumed_approval.id] = self._copy(consumed_approval)
            self._journal_approval(consumed_approval, "approval.consumed")
        if task.session_id is not None:
            self._journal(
                self.get_session(task.session_id),
                event_id=journal_event_id("effect.claimed", claimed.id, claimed.version),
                event_type="effect.claimed",
                task_id=task.id,
                trace_id=claimed.provider_call_id,
                data={"effect_id": claimed.id},
            )
        if claim_evaluation is not None:
            self._journal_policy(claimed, claim_evaluation.model_dump(mode="json"), "claimed")
        return claimed

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
        self._assert_guard(lease_guard)
        current = self._copy(self.effects[effect.id])
        self._check_version(effect.id, current.version, expected_version)
        if current.status is not EffectStatus.EXECUTING:
            raise ValueError(f"effect is not executing: {effect.id}")
        if effect.status not in {
            EffectStatus.SUCCEEDED,
            EffectStatus.FAILED,
            EffectStatus.UNKNOWN,
        }:
            raise ValueError(f"effect commit requires a terminal status: {effect.id}")
        if (call is None) is not (result is None):
            raise ValueError("Effect commit requires both call and result")
        if call is not None and result is not None:
            if call.id != current.provider_call_id or result.call_id != call.id:
                raise ValueError("tool observation does not belong to the Effect")
            key = (effect.task_id, call.id)
            existing = self.tool_results.get(key)
            if existing is not None and existing != (call, result):
                raise ValueError(f"tool call already committed with different content: {call.id}")
        committed = effect.model_copy(
            update={
                "status": effect.status,
                "result_ref": result_ref,
                "observation_ref": observation_ref,
                "version": current.version + 1,
            }
        )
        task = self.get_task(effect.task_id)
        if task.session_id is None:
            raise ValueError("effect task is not bound to a session")
        session = self.get_session(task.session_id)
        event = SessionEvent(
            id=effect_commit_event_id(committed.id, committed.version),
            session_id=session.id,
            task_id=task.id,
            trace_id=committed.provider_call_id,
            sequence=session.event_sequence + 1,
            type="effect.committed",
            data={
                "effect_id": committed.id,
                "status": committed.status.value,
                "result_ref": result_ref,
                "observation_ref": observation_ref,
            },
        )
        self.effects[effect.id] = self._copy(committed)
        if call is not None and result is not None:
            self.tool_results[(effect.task_id, call.id)] = (
                self._copy(call),
                self._copy(result),
            )
        self.events.setdefault(session.id, []).append(event)
        self.sessions[session.id] = session.model_copy(update={"event_sequence": event.sequence})
        checkpoint = self.checkpoints.get(task.id)
        if checkpoint is not None:
            self.checkpoints[task.id] = checkpoint.model_copy(
                update={
                    "event_sequence": event.sequence,
                    "pending_effect_ids": [
                        effect_id
                        for effect_id in checkpoint.pending_effect_ids
                        if effect_id != committed.id
                    ],
                }
            )
        return self._copy(committed)

    def settle_unexecuted_effect(
        self,
        effect_id: str,
        *,
        status: EffectStatus,
        expected_version: int,
        call: ToolCall,
        result: ToolResult,
        lease_guard: LeaseGuard,
    ) -> Effect:
        """Pair a denied or cancelled Effect with a non-execution observation."""

        self._assert_guard(lease_guard)
        current = self.get_effect(effect_id)
        self._check_version(effect_id, current.version, expected_version)
        if status not in {EffectStatus.DENIED, EffectStatus.CANCELLED}:
            raise ValueError("unexecuted Effect must be denied or cancelled")
        if current.status not in {
            EffectStatus.PREPARED,
            EffectStatus.WAITING_FOR_APPROVAL,
            status,
        }:
            raise ValueError(f"Effect cannot be settled from {current.status}")
        if call.id != current.provider_call_id or result.call_id != call.id:
            raise ValueError("tool observation does not belong to the Effect")
        key = (current.task_id, call.id)
        existing = self.tool_results.get(key)
        if existing is not None:
            if existing != (call, result) or current.status is not status:
                raise ValueError(f"tool call already committed with different content: {call.id}")
            return current
        settled = current.model_copy(
            update={
                "status": status,
                "result_ref": f"tool-result:{current.task_id}:{call.id}",
                "observation_ref": f"tool-result:{current.task_id}:{call.id}",
                "version": current.version + 1,
                "updated_at": datetime.now(UTC),
            }
        )
        if current.approval_id is not None:
            approval = self.get_approval(current.approval_id)
            if approval.status is ApprovalStatus.PENDING:
                self.approvals[approval.id] = approval.model_copy(
                    update={
                        "status": ApprovalStatus.EXPIRED,
                        "decision_source": "effect_settled",
                        "decided_at": datetime.now(UTC),
                        "version": approval.version + 1,
                    }
                )
        self.effects[current.id] = self._copy(settled)
        self.tool_results[key] = (self._copy(call), self._copy(result))
        task = self.get_task(current.task_id)
        if task.session_id is not None:
            session = self._journal(
                self.get_session(task.session_id),
                event_id=journal_event_id("effect.settled", settled.id, settled.version),
                event_type="effect.settled",
                task_id=task.id,
                trace_id=settled.provider_call_id,
                data={"effect_id": settled.id, "status": settled.status.value},
            )
            checkpoint = self.checkpoints.get(task.id)
            if checkpoint is not None:
                self.checkpoints[task.id] = checkpoint.model_copy(
                    update={
                        "event_sequence": session.event_sequence,
                        "pending_effect_ids": [
                            effect_id
                            for effect_id in checkpoint.pending_effect_ids
                            if effect_id != settled.id
                        ],
                    }
                )
        return self._copy(settled)

    def mark_effect_unknown(
        self,
        effect_id: str,
        *,
        expected_version: int,
        evidence: dict[str, object],
        lease_guard: LeaseGuard,
    ) -> tuple[Effect, Task]:
        self._assert_guard(lease_guard)
        current = self.get_effect(effect_id)
        self._check_version(effect_id, current.version, expected_version)
        if current.status not in {EffectStatus.EXECUTING, EffectStatus.UNKNOWN}:
            raise ValueError(f"Effect cannot require recovery from {current.status}")
        unknown = current
        if current.status is EffectStatus.EXECUTING:
            unknown = current.model_copy(
                update={
                    "status": EffectStatus.UNKNOWN,
                    "reconciliation_evidence": self._copy(evidence),
                    "version": current.version + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            self.effects[current.id] = self._copy(unknown)
        task = self.get_task(current.task_id)
        recovery_task = task
        if task.runtime_condition is not TaskRuntimeCondition.RECOVERY_REQUIRED:
            recovery_task = task.model_copy(
                update={
                    "runtime_condition": TaskRuntimeCondition.RECOVERY_REQUIRED,
                    "version": task.version + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            self.tasks[task.id] = self._copy(recovery_task)
        if task.session_id is not None:
            session = self._journal(
                self.get_session(task.session_id),
                event_id=journal_event_id("effect.recovery_required", unknown.id, unknown.version),
                event_type="effect.recovery_required",
                task_id=task.id,
                trace_id=unknown.provider_call_id,
                data={"effect_id": unknown.id, "evidence": self._copy(evidence)},
            )
            checkpoint = self.checkpoints.get(task.id)
            if checkpoint is not None:
                self.checkpoints[task.id] = checkpoint.model_copy(
                    update={
                        "event_sequence": session.event_sequence,
                        "pending_effect_ids": [
                            pending_id
                            for pending_id in checkpoint.pending_effect_ids
                            if pending_id != unknown.id
                        ],
                    }
                )
        return self._copy(unknown), self._copy(recovery_task)

    def commit_tool_result(
        self,
        task_id: str,
        call: ToolCall,
        result: ToolResult,
        *,
        expected_version: int | None = None,
        lease_guard: LeaseGuard | None = None,
    ) -> ToolResult:
        if result.call_id != call.id:
            raise ValueError("tool result does not belong to the call")
        task = self.get_task(task_id)
        self._require_session_guard(task, lease_guard)
        key = (task_id, call.id)
        existing = self.tool_results.get(key)
        if existing is not None:
            if existing != (call, result):
                raise ValueError(f"tool call already committed with different content: {call.id}")
            return self._copy(existing[1])
        self._check_version(task_id, task.version, expected_version)
        self.tool_results[key] = (self._copy(call), self._copy(result))
        if task.session_id is not None:
            self._journal(
                self.get_session(task.session_id),
                event_id=journal_event_id("tool_result.committed", call.id, task_id),
                event_type="tool_result.committed",
                task_id=task_id,
                trace_id=call.id,
                data={"call_id": call.id, "tool_name": call.name, "success": result.success},
            )
        return self._copy(result)

    def get_tool_result(self, task_id: str, call_id: str) -> ToolResult | None:
        existing = self.tool_results.get((task_id, call_id))
        return None if existing is None else self._copy(existing[1])

    def list_tool_results(self, task_id: str) -> list[ToolResult]:
        return [
            self._copy(result)
            for (candidate_task_id, _), (_, result) in self.tool_results.items()
            if candidate_task_id == task_id
        ]

    def _require_session_guard(self, task: Task, lease_guard: LeaseGuard | None) -> None:
        if task.session_id is None:
            return
        if lease_guard is None or lease_guard.task_id != task.id:
            raise LeaseLost(task.id)
        self._assert_guard(lease_guard)

    def request_control(self, request: ControlRequest, *, expected_version: int) -> ControlRequest:
        task = self.get_task(request.task_id)
        self._check_version(request.task_id, task.version, expected_version)
        if request.id in self.controls:
            raise ValueError(f"control request already exists: {request.id}")
        self.controls[request.id] = self._copy(request)
        if task.session_id is not None:
            self._journal(
                self.get_session(task.session_id),
                event_id=journal_event_id("control.requested", request.id, request.version),
                event_type="control.requested",
                task_id=task.id,
                data={"control_id": request.id, "kind": request.kind.value},
            )
        return self._copy(request)

    def settle_control(
        self,
        request_id: str,
        *,
        status: ControlStatus,
        expected_version: int,
        cleanup_info: str | None = None,
    ) -> ControlRequest:
        current = self._copy(self.controls[request_id])
        self._check_version(request_id, current.version, expected_version)
        current.transition(status, cleanup_info=cleanup_info)
        self.controls[request_id] = self._copy(current)
        task = self.get_task(current.task_id)
        if task.session_id is not None:
            self._journal(
                self.get_session(task.session_id),
                event_id=journal_event_id("control.updated", current.id, current.version),
                event_type="control.updated",
                task_id=task.id,
                data={"control_id": current.id, "status": current.status.value},
            )
        return self._copy(current)

    def get_pending_control(self, task_id: str) -> ControlRequest | None:
        pending = [
            request
            for request in self.controls.values()
            if request.task_id == task_id
            and request.status in {ControlStatus.REQUESTED, ControlStatus.ACKNOWLEDGED}
        ]
        if not pending:
            return None
        return self._copy(sorted(pending, key=lambda item: item.requested_at)[0])

    def get_control_request(self, request_id: str) -> ControlRequest:
        try:
            return self._copy(self.controls[request_id])
        except KeyError as exc:
            raise KeyError(f"control request not found: {request_id}") from exc

    def resolve_recovery(
        self,
        disposition: RecoveryDisposition,
        *,
        task_id: str,
        expected_version: int,
        confirmed_call: ToolCall | None = None,
        confirmed_result: ToolResult | None = None,
        retry_effect: Effect | None = None,
        retry_approval: Approval | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        existing = self.recoveries.get(disposition.id) or next(
            (
                candidate
                for candidate in self.recoveries.values()
                if candidate.unknown_effect_id == disposition.unknown_effect_id
            ),
            None,
        )
        if existing is not None:
            if existing != disposition:
                raise RecoveryRequired(task_id, disposition.unknown_effect_id)
            return self._copy(task)
        self._check_version(task_id, task.version, expected_version)
        if task.runtime_condition is not TaskRuntimeCondition.RECOVERY_REQUIRED:
            raise RecoveryRequired(task_id, disposition.unknown_effect_id)
        unknown = self.effects.get(disposition.unknown_effect_id)
        if (
            unknown is None
            or unknown.task_id != task_id
            or unknown.status is not EffectStatus.UNKNOWN
        ):
            raise RecoveryRequired(task_id, disposition.unknown_effect_id)
        target = validate_recovery_resolution(
            task,
            unknown,
            disposition,
            confirmed_call=confirmed_call,
            confirmed_result=confirmed_result,
            retry_effect=retry_effect,
            retry_approval=retry_approval,
        )
        if confirmed_call is not None and confirmed_result is not None:
            reference = f"tool-result:{task.id}:{confirmed_call.id}"
            resolved_effect = unknown.model_copy(
                update={
                    "status": (
                        EffectStatus.SUCCEEDED if confirmed_result.success else EffectStatus.FAILED
                    ),
                    "result_ref": reference,
                    "observation_ref": reference,
                    "reconciliation_evidence": self._copy(disposition.evidence),
                    "version": unknown.version + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            self.effects[unknown.id] = self._copy(resolved_effect)
            self.tool_results[(task.id, confirmed_call.id)] = (
                self._copy(confirmed_call),
                self._copy(confirmed_result),
            )
        if retry_effect is not None and retry_approval is not None:
            if retry_effect.id in self.effects or any(
                candidate.identity_key() == retry_effect.identity_key()
                for candidate in self.effects.values()
            ):
                raise EffectIdentityConflict(retry_effect.id, retry_effect.identity_key())
            waiting_retry = retry_effect.model_copy(
                update={
                    "status": EffectStatus.WAITING_FOR_APPROVAL,
                    "approval_id": retry_approval.id,
                }
            )
            self.effects[waiting_retry.id] = self._copy(waiting_retry)
            self.approvals[retry_approval.id] = self._copy(retry_approval)
            original_call = ToolCall(
                id=unknown.provider_call_id,
                name=unknown.tool_name,
                arguments=unknown.arguments_summary,
            )
            original_observation = ToolResult(
                call_id=original_call.id,
                tool_name=original_call.name,
                success=False,
                error_kind=ErrorKind.PERMISSION_DENIED,
                output=json.dumps(
                    {
                        "backend_result": "unknown",
                        "effect_status": EffectStatus.UNKNOWN.value,
                        "next_action": "execute_explicit_retry_after_approval",
                        "recovery_disposition": disposition.id,
                        "retry_effect_id": waiting_retry.id,
                    },
                    sort_keys=True,
                ),
            )
            self.tool_results[(task.id, original_call.id)] = (
                self._copy(original_call),
                self._copy(original_observation),
            )
        self.recoveries[disposition.id] = self._copy(disposition)
        task_updates: dict[str, object] = {
            "runtime_condition": target,
            "version": task.version + 1,
            "updated_at": datetime.now(UTC),
        }
        if disposition.kind is RecoveryDispositionKind.ABANDON:
            task_updates.update(
                {
                    "outcome": TaskOutcome.CANCELLED,
                    "status": TaskStatus.CANCELLED,
                }
            )
        updated = task.model_copy(update=task_updates)
        self.tasks[task_id] = self._copy(updated)
        if task.session_id is not None:
            session = self.get_session(task.session_id)
            if (
                disposition.kind is RecoveryDispositionKind.ABANDON
                and session.active_task_id == task.id
            ):
                session = session.model_copy(
                    update={"active_task_id": None, "version": session.version + 1}
                )
            self._journal(
                session,
                event_id=journal_event_id(
                    "recovery.resolved", disposition.id, disposition.kind.value
                ),
                event_type="recovery.resolved",
                task_id=task.id,
                data={
                    "disposition_id": disposition.id,
                    "effect_id": disposition.unknown_effect_id,
                    "kind": disposition.kind.value,
                },
            )
        return self._copy(updated)

    def get_recovery_disposition(self, disposition_id: str) -> RecoveryDisposition:
        try:
            return self._copy(self.recoveries[disposition_id])
        except KeyError as exc:
            raise KeyError(f"recovery disposition not found: {disposition_id}") from exc

    def get_recovery_disposition_for_effect(self, effect_id: str) -> RecoveryDisposition | None:
        disposition = next(
            (
                candidate
                for candidate in self.recoveries.values()
                if candidate.unknown_effect_id == effect_id
            ),
            None,
        )
        return None if disposition is None else self._copy(disposition)

    def commit_checkpoint(
        self,
        checkpoint: SessionCheckpoint,
        *,
        expected_version: int,
        lease_guard: LeaseGuard,
    ) -> SessionCheckpoint:
        self._assert_guard(lease_guard)
        task = self.get_task(checkpoint.task_id)
        self._check_version(checkpoint.task_id, task.version, expected_version)
        if task.session_id != checkpoint.session_id:
            raise ValueError("checkpoint session does not own its task")
        session = self.get_session(checkpoint.session_id)
        if checkpoint.event_sequence > session.event_sequence:
            raise ValueError("checkpoint event cursor is ahead of the session journal")
        turns = self.turns.get(session.id, [])
        max_turn_sequence = max((turn.sequence for turn in turns), default=0)
        if checkpoint.consumed_input_sequence > max_turn_sequence:
            raise ValueError("checkpoint input cursor is ahead of persisted turns")
        if checkpoint.turn_id is not None:
            turn = next((turn for turn in turns if turn.id == checkpoint.turn_id), None)
            if turn is None or turn.task_id not in {None, checkpoint.task_id}:
                raise ValueError("checkpoint turn is not part of its session/task")
        if len(checkpoint.pending_effect_ids) != len(set(checkpoint.pending_effect_ids)):
            raise ValueError("checkpoint pending Effect IDs must be unique")
        for effect_id in checkpoint.pending_effect_ids:
            effect = self.effects.get(effect_id)
            if effect is None or effect.task_id != checkpoint.task_id:
                raise ValueError("checkpoint references an Effect outside its task")
        referenced_requests = set(checkpoint.accounted_provider_request_ids)
        if checkpoint.pending_provider_request_id is not None:
            referenced_requests.add(checkpoint.pending_provider_request_id)
        for request_id in referenced_requests:
            request = self.provider_requests.get(request_id)
            if request is None or request.task_id != checkpoint.task_id:
                raise ValueError("checkpoint references a provider request outside its task")
            if request_id in checkpoint.accounted_provider_request_ids and request.response is None:
                raise ValueError("checkpoint accounts for a provider request without a response")
            if (
                request_id == checkpoint.pending_provider_request_id
                and request.status is ProviderRequestStatus.INVALIDATED
            ):
                raise ValueError("checkpoint references an invalidated provider request")
        for attempt_id in checkpoint.accounted_provider_attempt_ids:
            attempt = self.provider_attempts.get(attempt_id)
            request = None if attempt is None else self.provider_requests.get(attempt.request_id)
            if request is None or request.task_id != checkpoint.task_id:
                raise ValueError("checkpoint references a provider attempt outside its task")
        self.checkpoints[checkpoint.task_id] = self._copy(checkpoint)
        self._journal(
            self.get_session(checkpoint.session_id),
            event_id=journal_event_id(
                "checkpoint.committed", checkpoint.task_id, checkpoint.updated_at.isoformat()
            ),
            event_type="checkpoint.committed",
            task_id=checkpoint.task_id,
            data={"consumed_input_sequence": checkpoint.consumed_input_sequence},
        )
        return self._copy(checkpoint)

    def _journal(
        self,
        session: Session,
        *,
        event_id: str,
        event_type: str,
        task_id: str | None = None,
        trace_id: str | None = None,
        data: dict[str, object] | None = None,
    ) -> Session:
        event = SessionEvent(
            id=event_id,
            session_id=session.id,
            type=event_type,
            task_id=task_id,
            trace_id=trace_id,
            sequence=session.event_sequence + 1,
            data={} if data is None else data,
        )
        self.events.setdefault(session.id, []).append(event)
        updated = session.model_copy(update={"event_sequence": event.sequence})
        self.sessions[session.id] = self._copy(updated)
        return self._copy(updated)

    def _journal_approval(self, approval: Approval, event_type: str) -> None:
        effect = self.get_effect(approval.effect_id)
        task = self.get_task(effect.task_id)
        if task.session_id is None:
            return
        self._journal(
            self.get_session(task.session_id),
            event_id=journal_event_id(event_type, approval.id, approval.version),
            event_type=event_type,
            task_id=task.id,
            data={
                "approval_id": approval.id,
                "effect_id": effect.id,
                "status": approval.status.value,
                "supersedes_approval_id": approval.supersedes_approval_id,
                "scope_kind": approval.scope_kind.value,
                "grant_id": approval.grant_id,
                "policy_version": approval.policy_version,
                "config_version": approval.config_version,
                "resource_summary": approval.resource_summary,
            },
        )


__all__ = [
    "AdvanceResult",
    "AdvanceStatus",
    "ApprovalConflict",
    "ContractError",
    "ControlRequested",
    "EffectIdentityConflict",
    "EffectPort",
    "FakeStore",
    "InputRevisionConflict",
    "LeaseConflict",
    "LeaseGuard",
    "LeaseLost",
    "ProviderAttemptOutcome",
    "ProviderAttemptRecord",
    "ProviderAttemptStatus",
    "ProviderRequestConflict",
    "ProviderRequestRecord",
    "ProviderRequestStatus",
    "ProviderUsageStatus",
    "RecoveryRequired",
    "RuntimePort",
    "RuntimeStore",
    "SessionStore",
    "StaleVersion",
    "Store",
    "SubmissionConflict",
    "WorkspaceLeaseGuard",
]


def _provider_request_identity(record: ProviderRequestRecord) -> tuple[object, ...]:
    return (
        record.request_id,
        record.task_id,
        record.purpose,
        record.step_index,
        record.epoch_generation,
        record.input_revision,
        record.binding_fingerprint,
        record.input_digest,
    )


def _provider_usage_status(usage: ModelUsage) -> ProviderUsageStatus:
    if usage.input_tokens_reported is True or usage.output_tokens_reported is True:
        return ProviderUsageStatus.REPORTED
    return ProviderUsageStatus.UNKNOWN
