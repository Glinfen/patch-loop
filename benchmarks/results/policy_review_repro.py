"""Run with pytest to reproduce the approval/policy review findings."""

import os

import pytest

from patchloop.evaluation.policy_audit import PolicyBypassCase, run_policy_case
from patchloop.execution.policy import (
    ActionDescriptor,
    PolicyEngine,
    PolicyRule,
    digest,
    normalize_path,
)
from patchloop.tools.base import ToolContext


@pytest.mark.parametrize(
    "command", [["python", "-cprint(1)"], ["node", "--eval=console.log(1)"]]
)
def test_inline_interpreter_variants_remain_hard_denied(tmp_path, command):
    case = PolicyBypassCase(
        id="inline-interpreter-variant",
        action="execute",
        arguments={"command": command},
        expected_decision="deny",
        rule="allow",
    )
    result = run_policy_case(case, tmp_path)
    # The adapter only counts dispatches; it does not execute the command.
    assert result["actual_decision"] == "deny", result
    assert result["backend_calls"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive path contract")
def test_windows_deny_pattern_matches_case_insensitively(tmp_path):
    context = ToolContext(tmp_path)
    descriptor = ActionDescriptor(
        action="edit",
        tool_name="edit_file",
        workspace_ref=str(tmp_path),
        session_id="review-session",
        resources=(normalize_path("SRC/Main.py", context),),
        arguments_fingerprint=digest({}),
        side_effect=True,
        risk="medium",
    )
    rules = tuple(
        PolicyRule(
            id=identifier,
            source="project",
            workspace_ref=str(tmp_path),
            action="edit",
            resource_kind="path",
            pattern=pattern,
            effect=effect,
            policy_version="review-v1",
        )
        for identifier, pattern, effect in (
            ("deny-uppercase", "SRC/**", "deny"),
            ("allow-all", "**", "allow"),
        )
    )
    result = PolicyEngine().evaluate(
        descriptor, rules=rules, policy_version="review-v1", config_version="review-v1"
    )
    assert result.decision.value == "deny", result.model_dump(mode="json")
