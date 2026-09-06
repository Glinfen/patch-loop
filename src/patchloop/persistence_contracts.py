"""Backend-neutral SRF-01 persistence contracts and a deterministic fake store."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar

from patchloop.domain import SessionStatus, Task, TaskOutcome, TaskRuntimeCondition
from patchloop.execution.models import (
    Approval,
    ControlRequest,
    ControlStatus,
    Effect,
    EffectStatus,
    Execution,
    ExecutionStatus,
    RecoveryDisposition,
    RecoveryDispositionKind,
)
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


@dataclass(frozen=True)
class LeaseGuard:
    """Opaque execution fencing data required by execution-owned writes."""

    execution_id: str
    task_id: str
    token: str
    generation: int


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

    def start_task(self, session_id: str, task: Task, *, expected_version: int) -> Task: ...

    def request_pause(
        self, request: ControlRequest, *, expected_version: int
    ) -> ControlRequest: ...

    def request_cancel(
        self, request: ControlRequest, *, expected_version: int
    ) -> ControlRequest: ...

    def close_session(self, session_id: str, *, expected_version: int) -> Session: ...


class RuntimeStore(Protocol):
    def create_task(self, task: Task) -> Task: ...

    def get_task(self, task_id: str) -> Task: ...

    def update_task(self, task: Task, *, expected_version: int) -> Task: ...

    def claim_execution(
        self, execution: Execution, *, expected_version: int | None = None
    ) -> Execution: ...

    def prepare_effects(
        self,
        effects: Sequence[Effect],
        *,
        expected_version: int,
        lease_guard: LeaseGuard | None = None,
    ) -> list[Effect]: ...

    def decide_approval(
        self, approval: Approval, *, expected_version: int | None = None
    ) -> Approval: ...

    def claim_effect(
        self, effect_id: str, *, expected_version: int, lease_guard: LeaseGuard
    ) -> Effect: ...

    def commit_effect(
        self,
        effect: Effect,
        *,
        expected_version: int,
        result_ref: str | None,
        observation_ref: str | None,
        lease_guard: LeaseGuard,
    ) -> Effect: ...

    def request_control(
        self, request: ControlRequest, *, expected_version: int
    ) -> ControlRequest: ...

    def settle_control(
        self,
        request_id: str,
        *,
        status: ControlStatus,
        expected_version: int,
    ) -> ControlRequest: ...

    def resolve_recovery(
        self,
        disposition: RecoveryDisposition,
        *,
        task_id: str,
        expected_version: int,
    ) -> Task: ...

    def commit_checkpoint(
        self,
        checkpoint: SessionCheckpoint,
        *,
        expected_version: int,
        lease_guard: LeaseGuard,
    ) -> SessionCheckpoint: ...


class Store(SessionStore, RuntimeStore, Protocol):
    """Combined backend boundary for a composition root."""

    pass


class RuntimePort(Protocol):
    def advance(self, execution: Execution) -> AdvanceResult: ...


class EffectPort(Protocol):
    def prepare(self, execution: Execution, effects: Sequence[Effect]) -> list[Effect]: ...

    def execute(self, execution: Execution, effect: Effect) -> Effect: ...

    def reconcile(self, execution: Execution, effect: Effect) -> Effect: ...


class FakeStore:
    """Small in-memory store implementing SRF-01 semantics for service tests."""

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.tasks: dict[str, Task] = {}
        self.turns: dict[str, list[Turn]] = {}
        self.executions: dict[str, Execution] = {}
        self.effects: dict[str, Effect] = {}
        self.approvals: dict[str, Approval] = {}
        self.controls: dict[str, ControlRequest] = {}
        self.recoveries: dict[str, RecoveryDisposition] = {}
        self.checkpoints: dict[str, SessionCheckpoint] = {}

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
        return self._copy(session)

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

    def close_session(self, session_id: str, *, expected_version: int) -> Session:
        session = self.get_session(session_id)
        self._check_version(session.id, session.version, expected_version)
        if session.active_task_id is not None:
            raise ValueError("cannot close a session with an active task")
        closed = session.model_copy(
            update={"status": SessionStatus.CLOSED, "version": session.version + 1}
        )
        self.sessions[session_id] = self._copy(closed)
        return self._copy(closed)

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
        turns = self.turns.setdefault(session.id, [])
        if turn.client_submission_id is not None:
            for existing in turns:
                if existing.client_submission_id != turn.client_submission_id:
                    continue
                if existing.content == turn.content and existing.role is turn.role:
                    return self._copy(existing)
                raise SubmissionConflict(session.id, turn.client_submission_id)
        self._check_version(session.id, session.version, expected_version)
        assigned = turn.model_copy(
            update={"sequence": len(turns) + 1, "task_id": turn.task_id or session.active_task_id}
        )
        turns.append(assigned)
        self.sessions[session.id] = session.model_copy(
            update={"version": session.version + 1, "event_sequence": session.event_sequence + 1}
        )
        return self._copy(assigned)

    def create_task(self, task: Task) -> Task:
        if task.id in self.tasks:
            raise ValueError(f"task already exists: {task.id}")
        self.tasks[task.id] = self._copy(task)
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

    def get_effect(self, effect_id: str) -> Effect:
        try:
            return self._copy(self.effects[effect_id])
        except KeyError as exc:
            raise KeyError(f"effect not found: {effect_id}") from exc

    def list_effects(self, task_id: str) -> list[Effect]:
        return [self._copy(effect) for effect in self.effects.values() if effect.task_id == task_id]

    def get_checkpoint(self, task_id: str) -> SessionCheckpoint:
        try:
            return self._copy(self.checkpoints[task_id])
        except KeyError as exc:
            raise KeyError(f"checkpoint not found: {task_id}") from exc

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
        self.create_task(bound)
        self.sessions[session_id] = session.model_copy(
            update={"active_task_id": bound.id, "version": session.version + 1}
        )
        return self._copy(bound)

    def update_task(self, task: Task, *, expected_version: int) -> Task:
        current = self.get_task(task.id)
        self._check_version(task.id, current.version, expected_version)
        updated = task.model_copy(update={"version": current.version + 1})
        self.tasks[task.id] = self._copy(updated)
        if updated.outcome is not TaskOutcome.ACTIVE and updated.session_id is not None:
            session = self.get_session(updated.session_id)
            if session.active_task_id == updated.id:
                self.sessions[session.id] = session.model_copy(
                    update={"active_task_id": None, "version": session.version + 1}
                )
        return self._copy(updated)

    def claim_execution(
        self, execution: Execution, *, expected_version: int | None = None
    ) -> Execution:
        task = self.get_task(execution.task_id)
        self._check_version(task.id, task.version, expected_version)
        active = next(
            (
                item
                for item in self.executions.values()
                if item.task_id == execution.task_id
                and item.status
                in {
                    ExecutionStatus.RUNNING,
                    ExecutionStatus.CLAIMED,
                    ExecutionStatus.WAITING_FOR_APPROVAL,
                    ExecutionStatus.PAUSED,
                }
            ),
            None,
        )
        if active is not None:
            raise LeaseConflict(execution.task_id, active.owner_id)
        claimed = execution.model_copy(update={"status": ExecutionStatus.RUNNING})
        self.executions[claimed.id] = self._copy(claimed)
        self.tasks[task.id] = task.model_copy(
            update={
                "runtime_condition": TaskRuntimeCondition.RUNNING,
                "version": task.version + 1,
            }
        )
        return self._copy(claimed)

    def _assert_guard(self, guard: LeaseGuard) -> Execution:
        execution = self.executions.get(guard.execution_id)
        if (
            execution is None
            or execution.task_id != guard.task_id
            or execution.lease_token != guard.token
            or execution.generation != guard.generation
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
        if lease_guard is not None:
            self._assert_guard(lease_guard)
        task_ids = {effect.task_id for effect in effects}
        if len(task_ids) != 1:
            raise ValueError("one prepare batch must belong to one task")
        task_id = next(iter(task_ids))
        task = self.get_task(task_id)
        self._check_version(task_id, task.version, expected_version)
        result: list[Effect] = []
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
                self._check_version(task_id, task.version, expected_version)
                self.effects[effect.id] = self._copy(effect)
                result.append(self._copy(effect))
        return result

    def decide_approval(
        self, approval: Approval, *, expected_version: int | None = None
    ) -> Approval:
        existing = self.approvals.get(approval.id)
        if existing is None:
            self.approvals[approval.id] = self._copy(approval)
            return self._copy(approval)
        if existing.status is not approval.status:
            raise ApprovalConflict(approval.id, existing.status.value, approval.status.value)
        self._check_version(approval.id, existing.version, expected_version)
        return self._copy(existing)

    def claim_effect(
        self, effect_id: str, *, expected_version: int, lease_guard: LeaseGuard
    ) -> Effect:
        self._assert_guard(lease_guard)
        current = self._copy(self.effects[effect_id])
        self._check_version(effect_id, current.version, expected_version)
        if current.status is not EffectStatus.PREPARED:
            raise LeaseConflict(effect_id)
        claimed = current.model_copy(
            update={"status": EffectStatus.EXECUTING, "version": current.version + 1}
        )
        self.effects[effect_id] = self._copy(claimed)
        return claimed

    def commit_effect(
        self,
        effect: Effect,
        *,
        expected_version: int,
        result_ref: str | None,
        observation_ref: str | None,
        lease_guard: LeaseGuard,
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
        committed = effect.model_copy(
            update={
                "status": effect.status,
                "result_ref": result_ref,
                "observation_ref": observation_ref,
                "version": current.version + 1,
            }
        )
        self.effects[effect.id] = self._copy(committed)
        return self._copy(committed)

    def request_control(self, request: ControlRequest, *, expected_version: int) -> ControlRequest:
        task = self.get_task(request.task_id)
        self._check_version(request.task_id, task.version, expected_version)
        self.controls[request.id] = self._copy(request)
        return self._copy(request)

    def settle_control(
        self,
        request_id: str,
        *,
        status: ControlStatus,
        expected_version: int,
    ) -> ControlRequest:
        current = self._copy(self.controls[request_id])
        self._check_version(request_id, current.version, expected_version)
        current.transition(status)
        self.controls[request_id] = self._copy(current)
        return self._copy(current)

    def resolve_recovery(
        self,
        disposition: RecoveryDisposition,
        *,
        task_id: str,
        expected_version: int,
    ) -> Task:
        task = self.get_task(task_id)
        self._check_version(task_id, task.version, expected_version)
        if task.runtime_condition is not TaskRuntimeCondition.RECOVERY_REQUIRED:
            raise RecoveryRequired(task_id, disposition.unknown_effect_id)
        target = {
            RecoveryDispositionKind.CONFIRM_RESULT: TaskRuntimeCondition.RUNNING,
            RecoveryDispositionKind.CREATE_RETRY: TaskRuntimeCondition.WAITING_FOR_APPROVAL,
            RecoveryDispositionKind.ABANDON: TaskRuntimeCondition.ENDED,
        }[disposition.kind]
        disposition.validate_target(target)
        self.recoveries[disposition.id] = self._copy(disposition)
        updated = task.model_copy(update={"runtime_condition": target, "version": task.version + 1})
        self.tasks[task_id] = self._copy(updated)
        return self._copy(updated)

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
        self.checkpoints[checkpoint.task_id] = self._copy(checkpoint)
        return self._copy(checkpoint)


__all__ = [
    "AdvanceResult",
    "AdvanceStatus",
    "ApprovalConflict",
    "ContractError",
    "EffectIdentityConflict",
    "EffectPort",
    "FakeStore",
    "LeaseConflict",
    "LeaseGuard",
    "LeaseLost",
    "RecoveryRequired",
    "RuntimePort",
    "RuntimeStore",
    "SessionStore",
    "StaleVersion",
    "Store",
    "SubmissionConflict",
]
