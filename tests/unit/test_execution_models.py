from datetime import UTC, datetime, timedelta

import pytest

from patchloop.domain import TaskRuntimeCondition
from patchloop.execution.models import (
    Approval,
    ApprovalStatus,
    ControlKind,
    ControlRequest,
    ControlStatus,
    Effect,
    EffectStatus,
    Execution,
    RecoveryDisposition,
    RecoveryDispositionKind,
)
from patchloop.persistence_contracts import ApprovalConflict, EffectIdentityConflict


def _execution() -> Execution:
    return Execution(
        id="execution-1",
        session_id="session-1",
        task_id="task-1",
        owner_id="worker-1",
        lease_token="opaque-token",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )


def _effect(*, effect_id: str = "effect-1", step_id: str = "step-1", position: int = 0) -> Effect:
    return Effect(
        id=effect_id,
        task_id="task-1",
        step_id=step_id,
        batch_position=position,
        provider_call_id=f"provider-{effect_id}",
        tool_name="write_file",
        action_kind="write",
        arguments_summary={"path": "app.py"},
    )


def test_execution_and_effect_keep_provider_call_separate_from_identity() -> None:
    execution = _execution()
    effect = _effect()
    assert execution.generation == 1
    assert effect.identity_key() == ("task-1", "step-1", 0)
    assert effect.content_fingerprint()


def test_effect_identity_is_idempotent_by_effect_or_batch_position() -> None:
    original = _effect()
    same = original.model_copy(update={"arguments_summary": {"path": "other.py"}})
    with pytest.raises(EffectIdentityConflict):
        original.assert_identity_compatible(same)

    same_step = _effect(effect_id="effect-2")
    original.assert_identity_compatible(same_step)
    different_step = _effect(effect_id="effect-3", step_id="step-2")
    original.assert_identity_compatible(different_step)


def test_effect_lifecycle_rejects_replay_of_unknown() -> None:
    effect = _effect()
    effect.transition(EffectStatus.EXECUTING)
    effect.transition(EffectStatus.UNKNOWN)
    with pytest.raises(ValueError, match="invalid effect transition"):
        effect.transition(EffectStatus.PREPARED)


def test_approval_decision_is_explicit_and_conflicts_are_structured() -> None:
    approval = Approval(
        id="approval-1",
        effect_id="effect-1",
        action_summary="Write app.py",
        policy_version="policy-1",
        config_version="config-1",
    )
    approved = approval.decide(True, "operator")
    assert approved.status is ApprovalStatus.APPROVED
    assert approved.decide(True, "operator").status is ApprovalStatus.APPROVED
    with pytest.raises(ApprovalConflict):
        approved.decide(False, "operator-2")


def test_control_request_requires_acknowledge_before_settle() -> None:
    request = ControlRequest(id="control-1", task_id="task-1", kind=ControlKind.PAUSE)
    with pytest.raises(ValueError, match="invalid control transition"):
        request.transition(ControlStatus.SETTLED)
    request.transition(ControlStatus.ACKNOWLEDGED)
    request.transition(ControlStatus.SETTLED)
    assert request.status is ControlStatus.SETTLED


@pytest.mark.parametrize(
    ("kind", "condition"),
    [
        (RecoveryDispositionKind.CONFIRM_RESULT, TaskRuntimeCondition.RUNNING),
        (RecoveryDispositionKind.CREATE_RETRY, TaskRuntimeCondition.WAITING_FOR_APPROVAL),
        (RecoveryDispositionKind.ABANDON, TaskRuntimeCondition.ENDED),
    ],
)
def test_recovery_disposition_validates_explicit_target(
    kind: RecoveryDispositionKind, condition: TaskRuntimeCondition
) -> None:
    disposition = RecoveryDisposition(
        id=f"recovery-{kind.value}",
        unknown_effect_id="effect-1",
        kind=kind,
        decision_source="operator",
    )
    disposition.validate_target(condition)
