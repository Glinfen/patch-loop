"""Explicit disposition service for Effects whose external result is unknown."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from patchloop.domain import Task, TaskOutcome, TaskRuntimeCondition, ToolCall, ToolResult
from patchloop.execution.approvals import build_approval
from patchloop.execution.models import (
    Approval,
    ApprovalStatus,
    Effect,
    EffectStatus,
    RecoveryDisposition,
    RecoveryDispositionKind,
)


@dataclass(frozen=True)
class RecoveryResolution:
    task: Task
    disposition: RecoveryDisposition
    retry_effect: Effect | None = None
    retry_approval: Approval | None = None


class RecoveryStore(Protocol):
    def get_task(self, task_id: str) -> Task: ...

    def get_effect(self, effect_id: str) -> Effect: ...

    def list_effects(self, task_id: str) -> list[Effect]: ...

    def get_recovery_disposition_for_effect(self, effect_id: str) -> RecoveryDisposition | None: ...

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


class RecoveryService:
    """Require an explicit, evidenced decision before leaving recovery_required."""

    def __init__(self, store: RecoveryStore) -> None:
        self.store = store

    def pending(self, task_id: str) -> list[Effect]:
        """Return unknown Effects which still require an operator disposition."""

        return [
            effect
            for effect in self.store.list_effects(task_id)
            if effect.status is EffectStatus.UNKNOWN
            and self.store.get_recovery_disposition_for_effect(effect.id) is None
        ]

    def confirm_persisted_result(
        self,
        *,
        task_id: str,
        unknown_effect_id: str,
        success: bool,
        output: str,
        evidence: dict[str, object],
        decision_source: str,
    ) -> RecoveryResolution:
        """Confirm an operator-observed result using the persisted call identity."""

        task = self.store.get_task(task_id)
        effect = self.store.get_effect(unknown_effect_id)
        call = ToolCall(
            id=effect.provider_call_id,
            name=effect.tool_name,
            arguments=effect.arguments_summary,
        )
        return self.confirm_result(
            task_id=task.id,
            unknown_effect_id=effect.id,
            call=call,
            result=ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=success,
                output=output,
            ),
            evidence=evidence,
            decision_source=decision_source,
            expected_version=task.version,
        )

    def abandon_pending(
        self,
        *,
        task_id: str,
        unknown_effect_id: str,
        evidence: dict[str, object],
        decision_source: str,
    ) -> RecoveryResolution:
        task = self.store.get_task(task_id)
        return self.abandon(
            task_id=task.id,
            unknown_effect_id=unknown_effect_id,
            evidence=evidence,
            decision_source=decision_source,
            expected_version=task.version,
        )

    def retry_pending(
        self,
        *,
        task_id: str,
        unknown_effect_id: str,
        duplicate_risk_acknowledged: bool,
        evidence: dict[str, object],
        decision_source: str,
        policy_version: str,
        config_version: str,
    ) -> RecoveryResolution:
        """Create a new exact Effect and a fresh one-time Approval."""

        self._require_duplicate_risk_acknowledgement(duplicate_risk_acknowledged)
        task = self.store.get_task(task_id)
        unknown = self.store.get_effect(unknown_effect_id)
        retry = unknown.model_copy(
            update={
                "id": f"effect-retry-{uuid4().hex}",
                "step_id": f"recovery-retry-{uuid4().hex}",
                "provider_call_id": f"recovery-call-{uuid4().hex}",
                "retry_of_effect_id": unknown.id,
                "policy_result": {
                    **unknown.policy_result,
                    "decision": "require_approval",
                    "approval_required": True,
                },
                "status": EffectStatus.PREPARED,
                "approval_id": None,
                "approval_consumed": False,
                "consumed_grant_id": None,
                "policy_evaluation": None,
                "reconciliation_evidence": {},
                "result_ref": None,
                "observation_ref": None,
                "version": 1,
            }
        )
        approval = build_approval(
            task,
            retry,
            policy_version=policy_version,
            config_version=config_version,
        )
        return self.create_retry(
            task_id=task.id,
            unknown_effect_id=unknown.id,
            retry_effect=retry,
            approval=approval,
            duplicate_risk_acknowledged=duplicate_risk_acknowledged,
            evidence={**evidence, "duplicate_risk_acknowledged": True},
            decision_source=decision_source,
            expected_version=task.version,
        )

    def confirm_result(
        self,
        *,
        task_id: str,
        unknown_effect_id: str,
        call: ToolCall,
        result: ToolResult,
        evidence: dict[str, object],
        decision_source: str,
        expected_version: int,
    ) -> RecoveryResolution:
        disposition = RecoveryDisposition(
            unknown_effect_id=unknown_effect_id,
            kind=RecoveryDispositionKind.CONFIRM_RESULT,
            evidence=evidence,
            decision_source=decision_source,
        )
        task = self.store.resolve_recovery(
            disposition,
            task_id=task_id,
            expected_version=expected_version,
            confirmed_call=call,
            confirmed_result=result,
        )
        return RecoveryResolution(task=task, disposition=disposition)

    def abandon(
        self,
        *,
        task_id: str,
        unknown_effect_id: str,
        evidence: dict[str, object],
        decision_source: str,
        expected_version: int,
    ) -> RecoveryResolution:
        disposition = RecoveryDisposition(
            unknown_effect_id=unknown_effect_id,
            kind=RecoveryDispositionKind.ABANDON,
            evidence=evidence,
            decision_source=decision_source,
        )
        task = self.store.resolve_recovery(
            disposition,
            task_id=task_id,
            expected_version=expected_version,
        )
        return RecoveryResolution(task=task, disposition=disposition)

    def create_retry(
        self,
        *,
        task_id: str,
        unknown_effect_id: str,
        retry_effect: Effect,
        approval: Approval,
        duplicate_risk_acknowledged: bool,
        evidence: dict[str, object],
        decision_source: str,
        expected_version: int,
    ) -> RecoveryResolution:
        self._require_duplicate_risk_acknowledgement(duplicate_risk_acknowledged)
        disposition = RecoveryDisposition(
            unknown_effect_id=unknown_effect_id,
            kind=RecoveryDispositionKind.CREATE_RETRY,
            evidence={**evidence, "duplicate_risk_acknowledged": True},
            decision_source=decision_source,
            retry_effect_id=retry_effect.id,
        )
        task = self.store.resolve_recovery(
            disposition,
            task_id=task_id,
            expected_version=expected_version,
            retry_effect=retry_effect,
            retry_approval=approval,
        )
        return RecoveryResolution(
            task=task,
            disposition=disposition,
            retry_effect=retry_effect.model_copy(
                update={
                    "status": EffectStatus.WAITING_FOR_APPROVAL,
                    "approval_id": approval.id,
                }
            ),
            retry_approval=approval,
        )

    @staticmethod
    def _require_duplicate_risk_acknowledgement(acknowledged: bool) -> None:
        if not acknowledged:
            raise ValueError(
                "retry may duplicate an external side effect; explicit duplicate-risk "
                "acknowledgement is required"
            )


def validate_recovery_resolution(
    task: Task,
    unknown: Effect,
    disposition: RecoveryDisposition,
    *,
    confirmed_call: ToolCall | None,
    confirmed_result: ToolResult | None,
    retry_effect: Effect | None,
    retry_approval: Approval | None,
) -> TaskRuntimeCondition:
    """Validate a disposition and return its resulting runtime condition."""

    if task.runtime_condition is not TaskRuntimeCondition.RECOVERY_REQUIRED:
        raise ValueError("task does not require recovery")
    if unknown.task_id != task.id or unknown.status is not EffectStatus.UNKNOWN:
        raise ValueError("recovery target is not an unknown Effect for this task")
    if not disposition.evidence:
        raise ValueError("recovery disposition requires operator evidence")
    supplied_confirmation = confirmed_call is not None or confirmed_result is not None
    supplied_retry = retry_effect is not None or retry_approval is not None
    if disposition.kind is RecoveryDispositionKind.CONFIRM_RESULT:
        if confirmed_call is None or confirmed_result is None or supplied_retry:
            raise ValueError("confirm_result requires exactly one call/result pair")
        if (
            confirmed_call.id != unknown.provider_call_id
            or confirmed_call.name != unknown.tool_name
            or _arguments_fingerprint(confirmed_call.arguments)
            != (unknown.arguments_fingerprint or _arguments_fingerprint(unknown.arguments_summary))
            or confirmed_result.call_id != confirmed_call.id
            or confirmed_result.tool_name != confirmed_call.name
        ):
            raise ValueError("confirmed result does not belong to the unknown Effect")
        target = (
            TaskRuntimeCondition.IDLE
            if task.outcome is TaskOutcome.ACTIVE
            else TaskRuntimeCondition.ENDED
        )
    elif disposition.kind is RecoveryDispositionKind.CREATE_RETRY:
        if disposition.evidence.get("duplicate_risk_acknowledged") is not True:
            raise ValueError("create_retry requires explicit duplicate-risk acknowledgement")
        if task.outcome is not TaskOutcome.ACTIVE:
            raise ValueError("cannot retry an Effect for a terminal task")
        if retry_effect is None or retry_approval is None or supplied_confirmation:
            raise ValueError("create_retry requires exactly one Effect/Approval pair")
        if (
            disposition.retry_effect_id != retry_effect.id
            or retry_effect.id == unknown.id
            or retry_effect.task_id != task.id
            or retry_effect.retry_of_effect_id != unknown.id
            or retry_effect.status is not EffectStatus.PREPARED
            or retry_effect.preparation_error is not None
            or retry_effect.tool_name != unknown.tool_name
            or (
                retry_effect.arguments_fingerprint
                or _arguments_fingerprint(retry_effect.arguments_summary)
            )
            != (unknown.arguments_fingerprint or _arguments_fingerprint(unknown.arguments_summary))
        ):
            raise ValueError("retry Effect is not a new exact retry of the unknown Effect")
        if (
            retry_effect.policy_result.get("decision") != "require_approval"
            or retry_effect.policy_result.get("approval_required") is not True
        ):
            raise ValueError("retry Effect must require a new approval")
        if (
            retry_approval.effect_id != retry_effect.id
            or retry_approval.status is not ApprovalStatus.PENDING
            or retry_approval.effect_fingerprint != retry_effect.content_fingerprint()
            or retry_approval.arguments_fingerprint != retry_effect.arguments_fingerprint
            or retry_approval.workspace_ref != task.repository
        ):
            raise ValueError("retry Approval is not exactly bound to the retry Effect")
        target = TaskRuntimeCondition.WAITING_FOR_APPROVAL
    else:
        if supplied_confirmation or supplied_retry or disposition.retry_effect_id is not None:
            raise ValueError("abandon does not accept a result or retry")
        target = TaskRuntimeCondition.ENDED
    disposition.validate_target(target)
    return target


def _arguments_fingerprint(arguments: dict[str, object]) -> str:
    payload = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "RecoveryResolution",
    "RecoveryService",
    "RecoveryStore",
    "validate_recovery_resolution",
]
