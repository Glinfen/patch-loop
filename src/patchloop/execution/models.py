"""Serializable models for execution ownership, effects, and recovery."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from patchloop.domain import TaskRuntimeCondition
from patchloop.security import CredentialBinding
from patchloop.session.models import SessionCheckpoint

EXECUTION_SCHEMA_VERSION: Literal["1.0"] = "1.0"


def _now() -> datetime:
    return datetime.now(UTC)


class ExecutionStatus(StrEnum):
    CLAIMED = "claimed"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECOVERY_REQUIRED = "recovery_required"
    RELEASED = "released"


class EffectStatus(StrEnum):
    PREPARED = "prepared"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    CONSUMED = "consumed"
    DENIED = "denied"
    EXPIRED = "expired"


class ControlKind(StrEnum):
    PAUSE = "pause"
    CANCEL = "cancel"


class ControlStatus(StrEnum):
    REQUESTED = "requested"
    ACKNOWLEDGED = "acknowledged"
    SETTLED = "settled"
    CLEANUP_FAILED = "cleanup_failed"


class RecoveryDispositionKind(StrEnum):
    CONFIRM_RESULT = "confirm_result"
    CREATE_RETRY = "create_retry"
    ABANDON = "abandon"


class Execution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = EXECUTION_SCHEMA_VERSION
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    session_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1, max_length=256)
    lease_token: str = Field(min_length=1, max_length=512)
    generation: int = Field(default=1, ge=1)
    lease_expires_at: datetime
    status: ExecutionStatus = ExecutionStatus.CLAIMED
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def token(self) -> str:
        return self.lease_token

    def transition(self, target: ExecutionStatus) -> None:
        allowed = {
            ExecutionStatus.CLAIMED: {
                ExecutionStatus.RUNNING,
                ExecutionStatus.WAITING_FOR_APPROVAL,
                ExecutionStatus.PAUSED,
                ExecutionStatus.RECOVERY_REQUIRED,
                ExecutionStatus.RELEASED,
            },
            ExecutionStatus.RUNNING: {
                ExecutionStatus.WAITING_FOR_APPROVAL,
                ExecutionStatus.PAUSED,
                ExecutionStatus.COMPLETED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
                ExecutionStatus.RECOVERY_REQUIRED,
                ExecutionStatus.RELEASED,
            },
            ExecutionStatus.WAITING_FOR_APPROVAL: {
                ExecutionStatus.RUNNING,
                ExecutionStatus.PAUSED,
                ExecutionStatus.CANCELLED,
                ExecutionStatus.RECOVERY_REQUIRED,
                ExecutionStatus.RELEASED,
            },
            ExecutionStatus.PAUSED: {
                ExecutionStatus.RUNNING,
                ExecutionStatus.CANCELLED,
                ExecutionStatus.RELEASED,
            },
            ExecutionStatus.COMPLETED: set(),
            ExecutionStatus.FAILED: set(),
            ExecutionStatus.CANCELLED: set(),
            ExecutionStatus.RECOVERY_REQUIRED: {ExecutionStatus.RELEASED},
            ExecutionStatus.RELEASED: set(),
        }
        if target not in allowed[self.status]:
            raise ValueError(f"invalid execution transition: {self.status} -> {target}")
        object.__setattr__(self, "status", target)
        object.__setattr__(self, "version", self.version + 1)
        object.__setattr__(self, "updated_at", _now())


class WorkspaceLease(BaseModel):
    """One database-backed writer claim for a canonical workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = EXECUTION_SCHEMA_VERSION
    workspace_id: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    repository_path: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1, max_length=256)
    lease_token: str = Field(min_length=1, max_length=512)
    generation: int = Field(default=1, ge=1)
    lease_expires_at: datetime
    acquired_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class FileEffectPrecondition(BaseModel):
    """Persisted file state used to verify and reconstruct one mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    existed: bool
    original_content: str | None = Field(default=None, repr=False)
    original_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    target_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Effect(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal["1.0"] = EXECUTION_SCHEMA_VERSION
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    task_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    batch_position: int = Field(ge=0)
    provider_call_id: str = Field(min_length=1)
    retry_of_effect_id: str | None = Field(default=None, min_length=1)
    tool_name: str = Field(min_length=1, max_length=256)
    action_kind: str = Field(default="unknown", min_length=1, max_length=64)
    arguments_summary: dict[str, Any] = Field(default_factory=dict)
    arguments_fingerprint: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    policy_result: dict[str, Any] = Field(default_factory=dict)
    preparation_error: str | None = None
    file_preconditions: list[FileEffectPrecondition] = Field(default_factory=list)
    credential_bindings: list[CredentialBinding] = Field(default_factory=list)
    redacted_argument_paths: list[str] = Field(default_factory=list)
    status: EffectStatus = EffectStatus.PREPARED
    approval_id: str | None = Field(default=None, min_length=1)
    approval_consumed: bool = False
    reconciliation_evidence: dict[str, Any] = Field(default_factory=dict)
    result_ref: str | None = Field(default=None, min_length=1)
    observation_ref: str | None = Field(default=None, min_length=1)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    def identity_key(self) -> tuple[str, str, int]:
        return self.task_id, self.step_id, self.batch_position

    @property
    def params_summary(self) -> dict[str, Any]:
        return self.arguments_summary

    def content_fingerprint(self) -> str:
        argument_fingerprint = (
            self.arguments_fingerprint
            or hashlib.sha256(
                json.dumps(
                    self.arguments_summary,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        payload = {
            "action_kind": self.action_kind,
            "arguments_summary": self.arguments_summary,
            "arguments_fingerprint": argument_fingerprint,
            "credential_bindings": [
                binding.model_dump(mode="json") for binding in self.credential_bindings
            ],
            "redacted_argument_paths": self.redacted_argument_paths,
            "policy_result": self.policy_result,
            "preparation_error": self.preparation_error,
            "file_preconditions": [
                precondition.model_dump(mode="json") for precondition in self.file_preconditions
            ],
            "tool_name": self.tool_name,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def assert_identity_compatible(self, other: Effect) -> None:
        if self.id == other.id and self.identity_key() != other.identity_key():
            from patchloop.persistence_contracts import EffectIdentityConflict

            raise EffectIdentityConflict(self.id, self.identity_key())
        if (self.id == other.id or self.identity_key() == other.identity_key()) and (
            self.content_fingerprint() != other.content_fingerprint()
        ):
            from patchloop.persistence_contracts import EffectIdentityConflict

            raise EffectIdentityConflict(self.id, self.identity_key())

    def transition(self, target: EffectStatus) -> None:
        allowed = {
            EffectStatus.PREPARED: {
                EffectStatus.WAITING_FOR_APPROVAL,
                EffectStatus.EXECUTING,
                EffectStatus.DENIED,
            },
            EffectStatus.WAITING_FOR_APPROVAL: {
                EffectStatus.PREPARED,
                EffectStatus.DENIED,
                EffectStatus.CANCELLED,
            },
            EffectStatus.EXECUTING: {
                EffectStatus.SUCCEEDED,
                EffectStatus.FAILED,
                EffectStatus.UNKNOWN,
            },
            EffectStatus.SUCCEEDED: set(),
            EffectStatus.FAILED: set(),
            EffectStatus.DENIED: set(),
            EffectStatus.UNKNOWN: set(),
            EffectStatus.CANCELLED: set(),
        }
        if target not in allowed[self.status]:
            raise ValueError(f"invalid effect transition: {self.status} -> {target}")
        object.__setattr__(self, "status", target)
        object.__setattr__(self, "updated_at", _now())


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = EXECUTION_SCHEMA_VERSION
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    effect_id: str = Field(min_length=1)
    effect_fingerprint: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    arguments_fingerprint: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    workspace_ref: str = ""
    action_summary: str = Field(min_length=1, max_length=2_000)
    resource_summary: str = Field(default="", max_length=2_000)
    policy_version: str = Field(min_length=1, max_length=128)
    config_version: str = Field(min_length=1, max_length=128)
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision_source: str | None = Field(default=None, min_length=1, max_length=256)
    decided_at: datetime | None = None
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=_now)

    def same_request(self, other: Approval) -> bool:
        return (
            self.id == other.id
            and self.effect_id == other.effect_id
            and self.effect_fingerprint == other.effect_fingerprint
            and self.arguments_fingerprint == other.arguments_fingerprint
            and self.workspace_ref == other.workspace_ref
            and self.action_summary == other.action_summary
            and self.resource_summary == other.resource_summary
            and self.policy_version == other.policy_version
            and self.config_version == other.config_version
            and self.status is other.status is ApprovalStatus.PENDING
        )

    def matches_execution_conditions(
        self,
        effect: Effect,
        *,
        workspace_ref: str,
        policy_version: str,
        config_version: str,
    ) -> bool:
        return (
            self.effect_id == effect.id
            and self.effect_fingerprint == effect.content_fingerprint()
            and self.arguments_fingerprint == effect.arguments_fingerprint
            and self.workspace_ref == workspace_ref
            and self.policy_version == policy_version
            and self.config_version == config_version
        )

    def decide(self, approved: bool, source: str) -> Approval:
        target = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED
        if self.status is not ApprovalStatus.PENDING:
            if (
                approved
                and self.status is ApprovalStatus.CONSUMED
                and self.decision_source == source
            ):
                return self
            if self.status is target and self.decision_source == source:
                return self
            from patchloop.persistence_contracts import ApprovalConflict

            raise ApprovalConflict(self.id, self.status.value, target.value)
        return self.model_copy(
            update={
                "status": target,
                "decision_source": source,
                "decided_at": _now(),
                "version": self.version + 1,
            }
        )

    def consume(
        self,
        effect: Effect,
        *,
        workspace_ref: str,
        policy_version: str,
        config_version: str,
    ) -> Approval:
        """Consume an approved request after rechecking its exact execution binding."""

        if self.status is not ApprovalStatus.APPROVED:
            from patchloop.persistence_contracts import ApprovalConflict

            raise ApprovalConflict(
                self.id,
                self.status.value,
                ApprovalStatus.CONSUMED.value,
            )
        if not self.matches_execution_conditions(
            effect,
            workspace_ref=workspace_ref,
            policy_version=policy_version,
            config_version=config_version,
        ):
            from patchloop.persistence_contracts import ApprovalConflict

            raise ApprovalConflict(self.id, "binding_mismatch", ApprovalStatus.CONSUMED.value)
        return self.model_copy(
            update={
                "status": ApprovalStatus.CONSUMED,
                "version": self.version + 1,
            }
        )


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal["1.0"] = EXECUTION_SCHEMA_VERSION
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    task_id: str = Field(min_length=1)
    execution_id: str | None = Field(default=None, min_length=1)
    kind: ControlKind
    status: ControlStatus = ControlStatus.REQUESTED
    requested_at: datetime = Field(default_factory=_now)
    acknowledged_at: datetime | None = None
    settled_at: datetime | None = None
    cleanup_info: str | None = None
    version: int = Field(default=1, ge=1)

    def transition(self, target: ControlStatus, *, cleanup_info: str | None = None) -> None:
        allowed = {
            ControlStatus.REQUESTED: {
                ControlStatus.ACKNOWLEDGED,
                ControlStatus.CLEANUP_FAILED,
            },
            ControlStatus.ACKNOWLEDGED: {
                ControlStatus.SETTLED,
                ControlStatus.CLEANUP_FAILED,
            },
            ControlStatus.SETTLED: set(),
            ControlStatus.CLEANUP_FAILED: set(),
        }
        if target not in allowed[self.status]:
            raise ValueError(f"invalid control transition: {self.status} -> {target}")
        now = _now()
        object.__setattr__(self, "status", target)
        object.__setattr__(self, "version", self.version + 1)
        object.__setattr__(self, "cleanup_info", cleanup_info)
        if target is ControlStatus.ACKNOWLEDGED:
            object.__setattr__(self, "acknowledged_at", now)
        if target is ControlStatus.SETTLED:
            object.__setattr__(self, "settled_at", now)


class RecoveryDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = EXECUTION_SCHEMA_VERSION
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    unknown_effect_id: str = Field(min_length=1)
    kind: RecoveryDispositionKind
    evidence: dict[str, Any] = Field(default_factory=dict)
    decision_source: str = Field(min_length=1, max_length=256)
    retry_effect_id: str | None = Field(default=None, min_length=1)
    created_at: datetime = Field(default_factory=_now)

    def validate_target(self, runtime_condition: TaskRuntimeCondition) -> None:
        allowed = {
            RecoveryDispositionKind.CONFIRM_RESULT: {
                TaskRuntimeCondition.IDLE,
                TaskRuntimeCondition.RUNNING,
                TaskRuntimeCondition.PAUSED,
                TaskRuntimeCondition.ENDED,
            },
            RecoveryDispositionKind.CREATE_RETRY: {
                TaskRuntimeCondition.WAITING_FOR_APPROVAL,
            },
            RecoveryDispositionKind.ABANDON: {TaskRuntimeCondition.ENDED},
        }
        if runtime_condition not in allowed[self.kind]:
            raise ValueError(f"recovery disposition {self.kind} cannot produce {runtime_condition}")


__all__ = [
    "Approval",
    "ApprovalStatus",
    "ControlKind",
    "ControlRequest",
    "ControlStatus",
    "Effect",
    "EffectStatus",
    "Execution",
    "ExecutionStatus",
    "RecoveryDisposition",
    "RecoveryDispositionKind",
    "SessionCheckpoint",
    "WorkspaceLease",
]
