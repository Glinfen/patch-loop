from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from patchloop.domain import Task, TaskRuntimeCondition, TaskStatus, ToolCall, ToolResult
from patchloop.execution.approvals import build_approval
from patchloop.execution.effects import arguments_fingerprint
from patchloop.execution.models import (
    ApprovalStatus,
    Effect,
    EffectStatus,
    Execution,
)
from patchloop.execution.recovery import RecoveryService
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import FakeStore, LeaseGuard
from patchloop.security import PolicyDecision
from patchloop.session.models import Session


@pytest.fixture(params=["fake", "sqlite"])
def recovery_store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[object]:
    if request.param == "fake":
        yield FakeStore()
    else:
        yield SQLiteStore(tmp_path / "recovery.db")


def _unknown_effect(store: object) -> tuple[Task, Effect]:
    session = store.create_session(Session(id="session-1", workspace_ref="workspace"))
    task = store.start_task(
        session.id,
        Task(id="task-1", goal="Recover safely", repository="workspace"),
        expected_version=session.version,
    )
    execution = store.claim_execution(
        Execution(
            id="execution-1",
            session_id=session.id,
            task_id=task.id,
            owner_id="worker-1",
            lease_token="lease-token",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        ),
        expected_version=task.version,
    )
    guard = LeaseGuard(
        execution.id,
        task.id,
        execution.lease_token,
        execution.generation,
        execution.owner_id,
    )
    arguments = {"path": "result.txt"}
    prepared = store.prepare_effects(
        [
            Effect(
                id="effect-unknown",
                task_id=task.id,
                step_id="step-original",
                batch_position=0,
                provider_call_id="call-original",
                tool_name="external_action",
                action_kind="write",
                arguments_summary=arguments,
                arguments_fingerprint=arguments_fingerprint(arguments),
                policy_result={"decision": PolicyDecision.ALLOW.value},
            )
        ],
        expected_version=store.get_task(task.id).version,
        lease_guard=guard,
    )[0]
    claimed = store.claim_effect(
        prepared.id,
        expected_version=prepared.version,
        lease_guard=guard,
    )
    unknown, recovering = store.mark_effect_unknown(
        claimed.id,
        expected_version=claimed.version,
        evidence={"source": "result_missing"},
        lease_guard=guard,
    )
    return recovering, unknown


def test_confirm_result_persists_evidence_and_terminal_effect(recovery_store: object) -> None:
    task, unknown = _unknown_effect(recovery_store)
    call = ToolCall(
        id=unknown.provider_call_id,
        name=unknown.tool_name,
        arguments=unknown.arguments_summary,
    )
    result = ToolResult(
        call_id=call.id,
        tool_name=call.name,
        success=True,
        output="verified external result",
    )

    resolution = RecoveryService(recovery_store).confirm_result(
        task_id=task.id,
        unknown_effect_id=unknown.id,
        call=call,
        result=result,
        evidence={"ticket": "INC-42", "verified_by": "operator"},
        decision_source="operator",
        expected_version=task.version,
    )

    assert resolution.task.runtime_condition is TaskRuntimeCondition.IDLE
    resolved = recovery_store.get_effect(unknown.id)
    assert resolved.status is EffectStatus.SUCCEEDED
    assert resolved.reconciliation_evidence == resolution.disposition.evidence
    assert recovery_store.get_tool_result(task.id, call.id) == result
    assert (
        recovery_store.get_recovery_disposition(resolution.disposition.id) == resolution.disposition
    )


def test_persisted_recovery_helpers_do_not_require_callers_to_rebuild_effect_identity(
    recovery_store: object,
) -> None:
    task, unknown = _unknown_effect(recovery_store)
    service = RecoveryService(recovery_store)

    assert service.pending(task.id) == [unknown]

    resolution = service.confirm_persisted_result(
        task_id=task.id,
        unknown_effect_id=unknown.id,
        success=True,
        output="verified by operator",
        evidence={"ticket": "INC-43"},
        decision_source="cli",
    )

    assert resolution.task.runtime_condition is TaskRuntimeCondition.IDLE
    assert service.pending(task.id) == []
    result = recovery_store.get_tool_result(task.id, unknown.provider_call_id)
    assert result is not None
    assert result.output == "verified by operator"


def test_create_retry_keeps_unknown_and_requires_new_exact_approval(
    recovery_store: object,
) -> None:
    task, unknown = _unknown_effect(recovery_store)
    retry = Effect(
        id="effect-retry",
        task_id=task.id,
        step_id="step-recovery-retry",
        batch_position=0,
        provider_call_id="call-retry",
        retry_of_effect_id=unknown.id,
        tool_name=unknown.tool_name,
        action_kind=unknown.action_kind,
        arguments_summary=unknown.arguments_summary,
        arguments_fingerprint=unknown.arguments_fingerprint,
        policy_result={
            "decision": PolicyDecision.REQUIRE_APPROVAL.value,
            "approval_required": True,
        },
    )
    approval = build_approval(
        task,
        retry,
        policy_version="policy-1",
        config_version="1",
    )

    resolution = RecoveryService(recovery_store).create_retry(
        task_id=task.id,
        unknown_effect_id=unknown.id,
        retry_effect=retry,
        approval=approval,
        duplicate_risk_acknowledged=True,
        evidence={"duplicate_risk_acknowledged": True},
        decision_source="operator",
        expected_version=task.version,
    )

    assert resolution.task.runtime_condition is TaskRuntimeCondition.WAITING_FOR_APPROVAL
    assert recovery_store.get_effect(unknown.id).status is EffectStatus.UNKNOWN
    waiting = recovery_store.get_effect(retry.id)
    assert waiting.status is EffectStatus.WAITING_FOR_APPROVAL
    assert waiting.retry_of_effect_id == unknown.id
    assert waiting.approval_id == approval.id
    assert recovery_store.get_approval(approval.id).status is ApprovalStatus.PENDING
    original_observation = recovery_store.get_tool_result(task.id, unknown.provider_call_id)
    assert original_observation is not None
    assert '"backend_result": "unknown"' in original_observation.output
    assert retry.id in original_observation.output


def test_retry_pending_builds_a_new_exact_effect_and_approval(recovery_store: object) -> None:
    task, unknown = _unknown_effect(recovery_store)

    resolution = RecoveryService(recovery_store).retry_pending(
        task_id=task.id,
        unknown_effect_id=unknown.id,
        duplicate_risk_acknowledged=True,
        evidence={"duplicate_risk_acknowledged": True},
        decision_source="cli",
        policy_version="policy-1",
        config_version="1",
    )

    assert resolution.retry_effect is not None
    assert resolution.retry_effect.id != unknown.id
    assert resolution.retry_effect.retry_of_effect_id == unknown.id
    assert resolution.retry_effect.status is EffectStatus.WAITING_FOR_APPROVAL
    assert resolution.retry_approval is not None
    assert resolution.retry_approval.effect_id == resolution.retry_effect.id


def test_retry_pending_rejects_missing_duplicate_risk_acknowledgement_without_mutation(
    recovery_store: object,
) -> None:
    task, unknown = _unknown_effect(recovery_store)
    service = RecoveryService(recovery_store)

    with pytest.raises(ValueError, match="duplicate-risk acknowledgement"):
        service.retry_pending(
            task_id=task.id,
            unknown_effect_id=unknown.id,
            duplicate_risk_acknowledged=False,
            evidence={"operator_note": "retry requested"},
            decision_source="cli",
            policy_version="policy-1",
            config_version="1",
        )

    assert service.pending(task.id) == [unknown]
    assert recovery_store.list_effects(task.id) == [unknown]
    assert recovery_store.get_recovery_disposition_for_effect(unknown.id) is None


def test_abandon_cancels_task_but_preserves_unknown_evidence(recovery_store: object) -> None:
    task, unknown = _unknown_effect(recovery_store)

    resolution = RecoveryService(recovery_store).abandon(
        task_id=task.id,
        unknown_effect_id=unknown.id,
        evidence={"reason": "operator chose not to risk a duplicate"},
        decision_source="operator",
        expected_version=task.version,
    )

    assert resolution.task.status is TaskStatus.CANCELLED
    assert resolution.task.runtime_condition is TaskRuntimeCondition.ENDED
    assert recovery_store.get_effect(unknown.id).status is EffectStatus.UNKNOWN
    assert recovery_store.get_session("session-1").active_task_id is None
    assert (
        recovery_store.get_recovery_disposition(resolution.disposition.id).evidence
        == resolution.disposition.evidence
    )


def test_confirm_result_rejects_changed_action_without_mutation(recovery_store: object) -> None:
    task, unknown = _unknown_effect(recovery_store)

    with pytest.raises(ValueError, match="does not belong"):
        RecoveryService(recovery_store).confirm_result(
            task_id=task.id,
            unknown_effect_id=unknown.id,
            call=ToolCall(
                id=unknown.provider_call_id,
                name=unknown.tool_name,
                arguments={"path": "different.txt"},
            ),
            result=ToolResult(
                call_id=unknown.provider_call_id,
                tool_name=unknown.tool_name,
                success=True,
            ),
            evidence={"verified": True},
            decision_source="operator",
            expected_version=task.version,
        )

    assert (
        recovery_store.get_task(task.id).runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED
    )
    assert recovery_store.get_effect(unknown.id).status is EffectStatus.UNKNOWN
