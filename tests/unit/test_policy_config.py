import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import patchloop.cli as cli
from patchloop.domain import Task
from patchloop.execution.policy import PolicyAction, PolicyEngine, PolicyRuleEffect
from patchloop.execution.policy_config import PolicyConfigurationError, load_policy_configuration
from patchloop.tools import ReplaceTextTool, ToolContext


def config(path: Path, **overrides: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "enabled": True,
                "rules": [
                    {
                        "id": "files",
                        "action": "edit",
                        "resource_kind": "path",
                        "pattern": "**",
                        "effect": "ask",
                    }
                ],
                **overrides,
            }
        ),
        encoding="utf-8",
    )


def test_configuration_is_stable_and_changes_invalidate_policy_version(tmp_path: Path) -> None:
    config(tmp_path / ".patchloop" / "policy.json")
    task = Task(goal="edit", repository=str(tmp_path))
    first = cli._tool_policy(task)
    second = cli._tool_policy(task)
    assert first.version == second.version
    assert first.rules == second.rules
    assert first.rules[0].source == "project"
    assert first.rules[0].policy_version == first.version
    config(tmp_path / ".patchloop" / "policy.json", rules=[])
    assert cli._tool_policy(task).version != first.version


@pytest.mark.parametrize(
    "entry",
    [
        {"source": "system"},
        {"action": "unknown"},
        {"workspace_ref": "other"},
        {"resource_kind": "unknown"},
        {"pattern": "../*"},
        {"effect": "auto"},
    ],
)
def test_invalid_or_forged_rules_fail_closed(tmp_path: Path, entry: dict[str, object]) -> None:
    config(
        tmp_path / ".patchloop" / "policy.json",
        rules=[
            {
                "id": "bad",
                "action": "edit",
                "resource_kind": "path",
                "pattern": "**",
                "effect": "allow",
                **entry,
            }
        ],
    )
    with pytest.raises(PolicyConfigurationError):
        load_policy_configuration(tmp_path)


def test_system_deny_survives_project_allow(tmp_path: Path) -> None:
    system = tmp_path / "system.json"
    config(
        system,
        rules=[
            {
                "id": "deny",
                "action": "edit",
                "resource_kind": "path",
                "pattern": "**",
                "effect": "deny",
            }
        ],
    )
    config(
        tmp_path / ".patchloop" / "policy.json",
        rules=[
            {
                "id": "allow",
                "action": "edit",
                "resource_kind": "path",
                "pattern": "a.txt",
                "effect": "allow",
            }
        ],
    )
    snapshot = load_policy_configuration(tmp_path, system_path=system)
    tool = ReplaceTextTool()
    descriptor = tool.policy_descriptor(
        tool.input_model.model_validate({"path": "a.txt", "old_text": "a", "new_text": "b"}),
        ToolContext(tmp_path),
    )
    assert descriptor.action == PolicyAction.EDIT
    evaluation = PolicyEngine().evaluate(
        descriptor, rules=snapshot.rules_for("v1"), policy_version="v1", config_version="1"
    )
    assert evaluation.decision == PolicyRuleEffect.DENY
    assert evaluation.matched_rule_ids == ("system:deny",)


def test_invalid_policy_is_rejected_before_provider_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config(tmp_path / ".patchloop" / "policy.json", unexpected="password=hidden")
    calls: list[bool] = []
    monkeypatch.setattr(cli, "_selected_provider", lambda *args, **kwargs: calls.append(True))
    result = CliRunner().invoke(cli.app, ["run", "inspect", "--repo", str(tmp_path)])
    assert result.exit_code != 0
    assert calls == []
    assert "invalid project policy configuration" in result.output
    assert "hidden" not in result.output


def test_explicit_missing_configuration_is_not_silently_ignored(tmp_path: Path) -> None:
    with pytest.raises(PolicyConfigurationError, match="missing"):
        load_policy_configuration(tmp_path, system_path=tmp_path / "absent.json")
