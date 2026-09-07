import pytest

from patchloop.domain import Task
from patchloop.execution.approvals import (
    EffectPreauthorization,
    build_approval,
    match_exact_preauthorization,
)
from patchloop.execution.models import Effect


def _effect() -> Effect:
    return Effect(
        id="effect-1",
        task_id="task-1",
        step_id="step-1",
        batch_position=0,
        provider_call_id="provider-call-1",
        tool_name="write_file",
        action_kind="write",
        arguments_summary={"path": "README.md", "content": "updated\n"},
        arguments_fingerprint="a" * 64,
    )


def _preauthorization() -> EffectPreauthorization:
    return EffectPreauthorization(
        id="preauth-1",
        tool_name="write_file",
        arguments_fingerprint="a" * 64,
        workspace_ref="workspace",
        policy_version="policy-1",
        config_version="config-1",
    )


def test_exact_preauthorization_matches_every_execution_dimension() -> None:
    effect = _effect()
    exact = _preauthorization()

    assert (
        match_exact_preauthorization(
            effect,
            [
                exact.model_copy(update={"id": "wrong-tool", "tool_name": "create_file"}),
                exact.model_copy(
                    update={"id": "wrong-arguments", "arguments_fingerprint": "b" * 64}
                ),
                exact.model_copy(update={"id": "wrong-workspace", "workspace_ref": "other"}),
                exact.model_copy(update={"id": "wrong-policy", "policy_version": "policy-2"}),
                exact.model_copy(update={"id": "wrong-config", "config_version": "config-2"}),
                exact,
            ],
            workspace_ref="workspace",
            policy_version="policy-1",
            config_version="config-1",
        )
        == exact
    )


def test_preauthorization_is_not_a_fuzzy_or_reusable_match() -> None:
    effect = _effect()
    exact = _preauthorization()
    assert (
        match_exact_preauthorization(
            effect,
            [exact],
            workspace_ref="different-workspace",
            policy_version="policy-1",
            config_version="config-1",
        )
        is None
    )
    with pytest.raises(ValueError, match="multiple preauthorizations"):
        match_exact_preauthorization(
            effect,
            [exact, exact.model_copy(update={"id": "preauth-2"})],
            workspace_ref="workspace",
            policy_version="policy-1",
            config_version="config-1",
        )


def test_approval_binding_detects_effect_or_environment_changes() -> None:
    effect = _effect()
    task = Task(id="task-1", goal="Update", repository="workspace")
    approval = build_approval(
        task,
        effect,
        policy_version="policy-1",
        config_version="config-1",
    )

    assert approval.matches_execution_conditions(
        effect,
        workspace_ref="workspace",
        policy_version="policy-1",
        config_version="config-1",
    )
    assert not approval.matches_execution_conditions(
        effect.model_copy(update={"arguments_fingerprint": "b" * 64}),
        workspace_ref="workspace",
        policy_version="policy-1",
        config_version="config-1",
    )
