"""Executable policy bypass matrix with measured adapter calls and persisted evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from patchloop.domain import Task, ToolCall
from patchloop.events import EventLogger
from patchloop.execution.policy import (
    ActionDescriptor,
    PolicyAction,
    PolicyRule,
    PolicyRuleEffect,
    digest,
    normalize_command,
    normalize_network_target,
    normalize_package,
    normalize_path,
    normalize_skill,
)
from patchloop.persistence import SQLiteStore
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.runtime import AgentRuntime
from patchloop.security import PolicyDecision, RiskLevel, SecretRedactor
from patchloop.tools.base import PermissionLevel, Tool, ToolContext
from patchloop.tools.gateway import ToolGateway, ToolPolicy


class PolicyBypassCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    action: PolicyAction
    arguments: dict[str, Any]
    expected_decision: PolicyDecision
    rule: PolicyRuleEffect | None = None
    secret_sentinels: tuple[str, ...] = ()


class PolicyBypassMatrix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    cases: list[PolicyBypassCase] = Field(min_length=1)


class _AdapterInput(BaseModel):
    model_config = ConfigDict(extra="allow")


class _RecordingAdapter(Tool):
    name = "policy_audit_adapter"
    description = "Record a policy-authorized adapter invocation without external effects."
    permission = PermissionLevel.EXECUTE
    input_model = _AdapterInput

    def __init__(self, action: PolicyAction) -> None:
        self.action = action
        self.calls = 0

    def policy_descriptor(self, arguments: BaseModel, context: ToolContext) -> ActionDescriptor:
        data = arguments.model_dump(mode="json")
        if self.action == PolicyAction.NETWORK:
            resource = normalize_network_target(data["url"])
        elif self.action == PolicyAction.DEPENDENCY_INSTALL:
            resource = normalize_package(
                data["manager"], data["name"], data["version"], data["source"]
            )
        elif self.action in {PolicyAction.SKILL_LOAD, PolicyAction.SKILL_EXECUTE}:
            resource = normalize_skill(data["registry"], data["id"], data["version"])
        elif self.action in {PolicyAction.READ, PolicyAction.EDIT}:
            resource = normalize_path(data["path"], context)
        else:
            resource = normalize_command(data["command"])
        return ActionDescriptor(
            action=self.action,
            tool_name=self.name,
            workspace_ref=str(context.repository),
            session_id=context.session_id,
            resources=(resource,),
            arguments_fingerprint=digest(data),
            side_effect=True,
            risk=RiskLevel.HIGH,
        )

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        self.calls += 1
        return "Recorded adapter invocation; no external backend was contacted."


def run_policy_case(case: PolicyBypassCase, directory: Path) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    adapter = _RecordingAdapter(case.action)
    context = ToolContext(directory)
    policy = ToolPolicy(
        frozenset({PermissionLevel.EXECUTE}),
        require_plan_for_mutations=False,
        policy_extension=True,
    )
    call = ToolCall(id=f"call-{case.id}", name=adapter.name, arguments=case.arguments)
    if case.rule is not None:
        try:
            descriptor = adapter.policy_descriptor(
                _AdapterInput.model_validate(case.arguments), context
            )
        except ValueError:
            descriptor = None
        if descriptor is not None:
            policy.rules = tuple(
                PolicyRule(
                    id=f"audit-{case.id}-{index}",
                    source="system",
                    workspace_ref=str(directory),
                    action=case.action,
                    resource_kind=resource.kind,
                    pattern=resource.value,
                    effect=case.rule,
                    policy_version=policy.version,
                )
                for index, resource in enumerate(descriptor.resources)
            )
    logger = EventLogger(directory / "trace.jsonl")
    store = SQLiteStore(directory / "state.db")
    gateway = ToolGateway(context, [adapter], logger, policy)
    prepared = gateway.prepare_call("audit", call)
    task = AgentRuntime(
        FakeProvider([ModelResponse(tool_calls=[call]), ModelResponse(content="Done")]),
        gateway,
        logger,
        state_store=store,
    ).run(Task(goal="Verify policy boundary", repository=str(directory)))
    effects = store.list_effects(task.id)
    events = store.list_events(task.session_id or "")
    serialized = logger.path.read_text(encoding="utf-8") if hasattr(logger, "path") else ""
    serialized += "".join(event.model_dump_json() for event in events)
    serialized += "".join(effect.model_dump_json() for effect in effects)
    leaks = sum(secret in serialized for secret in case.secret_sentinels)
    expected_calls = 1 if case.expected_decision is PolicyDecision.ALLOW else 0
    actual_decision = prepared.policy_result.decision
    event_types = {event.type for event in events}
    evidence_complete = bool(effects) and (
        "policy.evaluated" in event_types
        or (
            actual_decision is PolicyDecision.DENY
            and any(event.type == "security.decision" for event in logger.read())
        )
    )
    if case.expected_decision is PolicyDecision.REQUIRE_APPROVAL:
        evidence_complete = evidence_complete and "approval.requested" in event_types
    reasons = []
    if actual_decision != case.expected_decision:
        reasons.append("decision_mismatch")
    if adapter.calls != expected_calls:
        reasons.append("backend_call_count_mismatch")
    if leaks:
        reasons.append("secret_leak")
    if not evidence_complete:
        reasons.append("missing_policy_or_approval_evidence")
    return {
        "case_id": case.id,
        "input": SecretRedactor().redact(case.arguments),
        "action": case.action.value,
        "expected_decision": case.expected_decision.value,
        "actual_decision": None if actual_decision is None else actual_decision.value,
        "backend_calls": adapter.calls,
        "expected_backend_calls": expected_calls,
        "unauthorized_actions": adapter.calls if expected_calls == 0 else 0,
        "duplicate_effects": max(0, adapter.calls - expected_calls),
        "secret_leaks": leaks,
        "event_integrity": evidence_complete,
        "event_types": sorted(event_types),
        "policy_version": policy.version,
        "config_version": context.config_version,
        "policy_fingerprint": digest(
            {"version": policy.version, "rules": [r.model_dump(mode="json") for r in policy.rules]}
        ),
        "config_fingerprint": digest({"config_version": context.config_version}),
        "matched_rule_ids": []
        if prepared.policy_evaluation is None
        else list(prepared.policy_evaluation.matched_rule_ids),
        "passed": not reasons,
        "failure_reasons": reasons,
        "external_backend_evidence": "unverified",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    matrix = PolicyBypassMatrix.model_validate_json(args.matrix.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="patchloop-policy-audit-") as temporary:
        results = [run_policy_case(case, Path(temporary) / case.id) for case in matrix.cases]
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    report = {
        "schema_version": "1.0",
        "revision": revision,
        "working_tree_fingerprint": working_tree_fingerprint(),
        "matrix_fingerprint": digest(matrix.model_dump(mode="json")),
        "passed": all(result["passed"] for result in results),
        "results": results,
        "totals": {
            name: sum(result[name] for result in results)
            for name in (
                "backend_calls",
                "unauthorized_actions",
                "duplicate_effects",
                "secret_leaks",
            )
        },
        "external_backend_evidence": "unverified",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if report["passed"] else 1


def working_tree_fingerprint() -> str:
    """Hash actual implementation and test bytes, including new untracked files."""
    paths = (
        subprocess.run(
            [
                "git",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                "src",
                "tests",
                "pyproject.toml",
                "README.md",
            ],
            check=True,
            capture_output=True,
        )
        .stdout.decode("utf-8")
        .split("\0")
    )
    return digest(
        {
            name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            if Path(name).is_file()
            else None
            for name in sorted(set(paths))
            if name
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
