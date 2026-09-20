from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import patchloop.execution.policy as policy_module
from patchloop.execution.policy import (
    ActionDescriptor,
    ApprovalGrant,
    ApprovalScopeKind,
    PolicyAction,
    PolicyEngine,
    PolicyNormalizationError,
    PolicyRule,
    PolicyRuleEffect,
    ResourceKind,
    ResourceSelector,
    canonical_json,
    digest,
    normalize_command,
    normalize_network_target,
    normalize_package,
    normalize_path,
    normalize_skill,
)
from patchloop.security import PolicyDecision, RiskLevel
from patchloop.tools.base import ToolContext


def descriptor(*paths: str) -> ActionDescriptor:
    return ActionDescriptor(
        action=PolicyAction.EDIT,
        tool_name="write_file",
        workspace_ref="workspace",
        session_id="session",
        resources=tuple(ResourceSelector(kind=ResourceKind.PATH, value=path) for path in paths),
        arguments_fingerprint=digest({"content": "new"}),
        side_effect=True,
        risk=RiskLevel.MEDIUM,
    )


def rule(name: str, pattern: str, effect: PolicyRuleEffect, **kwargs: object) -> PolicyRule:
    return PolicyRule.model_validate(
        {
            "id": name,
            "pattern": pattern,
            "effect": effect,
            "source": "project",
            "workspace_ref": "workspace",
            "action": "edit",
            "resource_kind": "path",
            "policy_version": "p1",
            **kwargs,
        }
    )


def test_canonical_descriptor_order_and_digest() -> None:
    first = descriptor("b.py", "a.py", "a.py")
    assert first == descriptor("a.py", "b.py")
    assert first.digest == ActionDescriptor.model_validate_json(first.model_dump_json()).digest
    assert digest({"b": 2, "a": 1}) == digest({"a": 1, "b": 2})


def test_resource_digest_cannot_be_forged() -> None:
    with pytest.raises(ValueError, match="digest mismatch"):
        ResourceSelector(kind=ResourceKind.PATH, value="a", digest="0" * 64)


def test_path_normalization_and_escape(tmp_path: Path) -> None:
    context = ToolContext(tmp_path)
    assert normalize_path("src\\main.py", context) == normalize_path("src/main.py", context)
    with pytest.raises(PolicyNormalizationError, match="escapes"):
        normalize_path("../outside", context)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["sh", "x"],
        ["curl", "https://example.org"],
        ["python", "-c", "x"],
        ["git", "push"],
        ["echo", "x>file"],
        ["echo", "a&&b"],
        ["echo", "password=hunter2"],
        ["echo", "credential://vault/key"],
    ],
)
def test_unsafe_commands_have_safe_errors(argv: list[str]) -> None:
    with pytest.raises(PolicyNormalizationError) as error:
        normalize_command(argv)
    assert "hunter2" not in str(error.value)


def test_network_normalization() -> None:
    assert normalize_network_target("https://EXAMPLE.org:443/a?q=1#x") == (
        normalize_network_target("https://example.org/a?q=2")
    )
    for target in ["relative", "ftp://example.org", "https://user:pass@example.org"]:
        with pytest.raises(PolicyNormalizationError):
            normalize_network_target(target)


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-cprint(1)"],
        ["python3.12", "-Icprint(1)"],
        ["C:/Python/python.exe", "-OOcprint(1)"],
        ["py", "-3", "-cprint(1)"],
        ["node", "--eval=console.log(1)"],
        ["node", "-econsole.log(1)"],
        ["node", "--print=1"],
        ["node", "-p1"],
        ["node", "-pe", "1"],
        ["ruby", "-neputs(1)"],
        ["perl", "-weprint(1)"],
        ["perl", "-E", "say(1)"],
    ],
)
def test_inline_interpreter_variants_cannot_be_allowed_or_granted(argv: list[str]) -> None:
    with pytest.raises(PolicyNormalizationError, match="inline interpreter"):
        normalize_command(argv)
    # Also check a previously persisted selector against current hard rules.
    resource = ResourceSelector(kind=ResourceKind.COMMAND, value=canonical_json(argv))
    action = descriptor("a.py").model_copy(
        update={"action": PolicyAction.EXECUTE, "resources": (resource,)}
    )
    allow = rule(
        "allow-command",
        resource.value,
        PolicyRuleEffect.ALLOW,
        action="execute",
        resource_kind="command",
    )
    grant = ApprovalGrant(
        id="old-grant",
        source_approval_id="old-approval",
        scope_kind="session",
        session_id=action.session_id,
        workspace_ref=action.workspace_ref,
        action=action.action,
        resources=action.resources,
        arguments_fingerprint=action.arguments_fingerprint,
        tool_name=action.tool_name,
        policy_version="p1",
        config_version="c1",
    )
    for rules, grants in [((allow,), ()), ((), (grant,))]:
        result = PolicyEngine().evaluate(
            action, rules=rules, grants=grants, policy_version="p1", config_version="c1"
        )
        assert result.decision is PolicyDecision.DENY
        assert result.matched_grant_id is None


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-m", "pytest", "-q"],
        ["python", "-I", "-m", "unittest"],
        ["node", "--version"],
        ["ruby", "--version"],
        ["perl", "-v"],
    ],
)
def test_safe_interpreter_commands_still_normalize(argv: list[str]) -> None:
    assert normalize_command(argv).value == canonical_json(argv)


@pytest.mark.parametrize("platform", ["nt", "posix"])
@pytest.mark.parametrize("pattern", ["SRC/**", "SRC/[A-Z]*.PY"])
def test_path_rule_case_matches_platform_contract(monkeypatch, platform, pattern) -> None:
    # Replace the module reference, not global os.name used by pathlib/pytest.
    monkeypatch.setattr(policy_module, "os", SimpleNamespace(name=platform))
    result = PolicyEngine().evaluate(
        descriptor("src/main.py"),
        rules=(
            rule("deny", pattern, PolicyRuleEffect.DENY),
            rule("allow", "**", PolicyRuleEffect.ALLOW),
        ),
        policy_version="p1",
        config_version="c1",
    )
    assert result.decision is (PolicyDecision.DENY if platform == "nt" else PolicyDecision.ALLOW)
    assert result.matched_rule_ids == (("deny",) if platform == "nt" else ("allow",))


def test_package_and_skill_versions_remain_distinct() -> None:
    assert normalize_package("pip", "demo", "1", "index") != normalize_package(
        "pip", "demo", "2", "index"
    )
    assert normalize_skill("registry", "demo", "1") != normalize_skill("registry", "demo", "2")


def test_multiresource_deny_and_ask_aggregation() -> None:
    engine = PolicyEngine()
    allow = rule("allow", "**", PolicyRuleEffect.ALLOW)
    ask = rule("ask", "b.py", PolicyRuleEffect.ASK)
    deny = rule("deny", "a.py", PolicyRuleEffect.DENY)
    assert engine.evaluate(
        descriptor("a.py", "b.py"), rules=(allow, ask), policy_version="p1", config_version="c1"
    ).decision == (PolicyDecision.REQUIRE_APPROVAL)
    assert (
        engine.evaluate(
            descriptor("a.py", "b.py"),
            rules=(allow, ask, deny),
            policy_version="p1",
            config_version="c1",
        ).decision
        == PolicyDecision.DENY
    )


def test_grants_are_exact_and_deny_still_wins() -> None:
    action = descriptor("a.py")
    grant = ApprovalGrant(
        id="grant",
        source_approval_id="approval",
        scope_kind=ApprovalScopeKind.SESSION,
        session_id=action.session_id,
        workspace_ref=action.workspace_ref,
        action=action.action,
        resources=action.resources,
        arguments_fingerprint=action.arguments_fingerprint,
        tool_name=action.tool_name,
        policy_version="p1",
        config_version="c1",
    )
    assert grant.matches(action, "p1", "c1")
    for changed in [
        action.model_copy(update={"session_id": "other"}),
        descriptor("b.py"),
        action.model_copy(update={"arguments_fingerprint": digest("other")}),
    ]:
        assert not grant.matches(changed, "p1", "c1")
    assert not grant.matches(action, "p2", "c1")
    expired = grant.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    assert not expired.matches(action, "p1", "c1")
    result = PolicyEngine().evaluate(
        action,
        grants=(grant,),
        rules=(rule("deny", "**", PolicyRuleEffect.DENY),),
        policy_version="p1",
        config_version="c1",
    )
    assert result.decision == PolicyDecision.DENY
    assert result.matched_grant_id is None


def test_default_side_effect_asks_and_source_tie_is_deterministic() -> None:
    engine = PolicyEngine()
    action = descriptor("a.py")
    assert engine.evaluate(action, policy_version="p1", config_version="c1").decision == (
        PolicyDecision.REQUIRE_APPROVAL
    )
    rules = (
        rule("z", "**", PolicyRuleEffect.ASK, source="session", session_id="session"),
        rule("a", "**", PolicyRuleEffect.ALLOW),
    )
    result = engine.evaluate(action, rules=rules, policy_version="p1", config_version="c1")
    assert result.matched_rule_ids == ("z",)
    assert result.decision == PolicyDecision.REQUIRE_APPROVAL


@pytest.mark.parametrize("value", [".env", ".env.local", ".git/config", ".ssh/id_ed25519"])
def test_raw_selector_cannot_name_sensitive_path(value: str) -> None:
    with pytest.raises(ValueError, match="control-plane path"):
        ResourceSelector(kind=ResourceKind.PATH, value=value)


@pytest.mark.parametrize(
    "action,kind,value",
    [
        (PolicyAction.NETWORK, ResourceKind.NETWORK_TARGET, '{"host":"example.org"}'),
        (PolicyAction.DEPENDENCY_INSTALL, ResourceKind.PACKAGE, '["pip","demo"]'),
        (PolicyAction.SKILL_EXECUTE, ResourceKind.SKILL, '["registry","demo"]'),
    ],
)
def test_raw_external_selector_fails_closed(action, kind, value) -> None:
    request = descriptor("a.txt").model_copy(
        update={"action": action, "resources": (ResourceSelector(kind=kind, value=value),)}
    )
    result = PolicyEngine().evaluate(request, policy_version="p1", config_version="c1")
    assert result.decision is PolicyDecision.DENY


def test_digest_is_stable_in_a_fresh_python_process() -> None:
    import subprocess
    import sys

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from patchloop.execution.policy import digest; print(digest({'b':2,'a':1}))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == digest({"a": 1, "b": 2})


def test_windows_junction_cannot_escape_workspace(tmp_path: Path) -> None:
    import os
    import subprocess

    if os.name != "nt":
        pytest.skip("Windows junction test")
    repository = tmp_path / "repository"
    outside = tmp_path / "outside"
    repository.mkdir()
    outside.mkdir()
    junction = repository / "escape"
    command = "New-Item -ItemType Junction -Path '" + str(junction).replace("'", "''")
    command += "' -Target '" + str(outside).replace("'", "''") + "' | Out-Null"
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        pytest.skip("Windows host could not create junction: " + completed.stderr)
    try:
        with pytest.raises(PolicyNormalizationError, match="escapes repository"):
            normalize_path("escape/target.txt", ToolContext(repository))
    finally:
        junction.rmdir()
