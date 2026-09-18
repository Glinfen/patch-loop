"""Stable, secret-safe contracts shared by policy and approval decisions.

This module deliberately contains no policy decision logic.  It defines the
immutable input which later policy engines evaluate and the canonicalizers
which make that input safe to persist and compare across processes.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.security import RISK_ORDER, PolicyDecision, RiskLevel, SecretRedactor


class PolicyAction(StrEnum):
    READ = "read"
    EDIT = "edit"
    EXECUTE = "execute"
    NETWORK = "network"
    DEPENDENCY_INSTALL = "dependency_install"
    SKILL_LOAD = "skill_load"
    SKILL_EXECUTE = "skill_execute"
    GIT = "git"


class ResourceKind(StrEnum):
    PATH = "path"
    COMMAND = "command"
    NETWORK_TARGET = "network_target"
    PACKAGE = "package"
    SKILL = "skill"
    WORKSPACE = "workspace"


class PolicyRuleEffect(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PolicySource(StrEnum):
    BUILTIN = "builtin"
    SYSTEM = "system"
    USER = "user"
    PROJECT = "project"
    SESSION = "session"


class ApprovalScopeKind(StrEnum):
    ONCE = "once"
    SESSION = "session"
    RESOURCE = "resource"


class GrantStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"


class PolicyNormalizationError(ValueError):
    """A descriptor cannot safely be converted into an authorization input."""

    def __init__(self, reason: str, *, code: str = "invalid_resource") -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ResourceSelector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ResourceKind
    value: str = Field(min_length=1)
    digest: str | None = None

    @model_validator(mode="after")
    def set_digest(self) -> ResourceSelector:
        expected = _digest({"kind": self.kind.value, "value": self.value})
        if self.digest is not None and self.digest != expected:
            raise ValueError("resource digest does not match canonical value")
        object.__setattr__(self, "digest", expected)
        return self


class ActionDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: PolicyAction
    tool_name: str = Field(min_length=1)
    workspace_ref: str = Field(min_length=1)
    session_id: str = ""
    resources: tuple[ResourceSelector, ...] = ()
    arguments_fingerprint: str = Field(min_length=1)
    side_effect: bool
    risk: RiskLevel

    @model_validator(mode="after")
    def sort_resources(self) -> ActionDescriptor:
        ordered = tuple(sorted(self.resources, key=lambda item: (item.kind.value, item.value)))
        object.__setattr__(self, "resources", ordered)
        return self

    @property
    def canonical_payload(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "tool_name": self.tool_name,
            "workspace_ref": self.workspace_ref,
            "session_id": self.session_id,
            "resources": [item.model_dump(mode="json") for item in self.resources],
            "arguments_fingerprint": self.arguments_fingerprint,
            "side_effect": self.side_effect,
            "risk": self.risk.value,
        }

    @property
    def digest(self) -> str:
        return _digest(self.canonical_payload)

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


class PolicyEvaluation(BaseModel):
    """Serializable evidence emitted alongside a policy decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: PolicyDecision
    risk: RiskLevel
    reason: str
    descriptor: ActionDescriptor
    matched_rule_ids: tuple[str, ...] = ()
    matched_grant_id: str | None = None
    policy_version: str
    config_version: str


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    schema_version: int = 1
    source: PolicySource
    workspace_ref: str | None = None
    session_id: str | None = None
    action: PolicyAction
    resource_kind: ResourceKind | None = None
    pattern: str = "*"
    effect: PolicyRuleEffect
    max_risk: RiskLevel = RiskLevel.CRITICAL
    priority: int = 0
    enabled: bool = True
    policy_version: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ApprovalGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    schema_version: int = 1
    source_approval_id: str = ""
    scope_kind: ApprovalScopeKind
    session_id: str | None = None
    workspace_ref: str
    action: PolicyAction
    resources: tuple[ResourceSelector, ...] = ()
    policy_version: str
    config_version: str
    expires_at: datetime | None = None
    remaining_uses: int | None = None
    status: GrantStatus = GrantStatus.ACTIVE
    version: int = 1

    def is_active(self, now: datetime | None = None) -> bool:
        if self.status is not GrantStatus.ACTIVE:
            return False
        if self.expires_at is not None:
            current = now or datetime.now(UTC)
            expiry = self.expires_at
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if expiry <= current:
                return False
        return self.remaining_uses is None or self.remaining_uses > 0


_SOURCE_ORDER = {
    PolicySource.SESSION: 5,
    PolicySource.PROJECT: 4,
    PolicySource.USER: 3,
    PolicySource.SYSTEM: 2,
    PolicySource.BUILTIN: 1,
}


class PolicyEngine:
    """Deterministic evaluator used by both preparation and execution paths."""

    def __init__(
        self,
        *,
        allowed_permissions: frozenset[str] | None = None,
        approval_threshold: RiskLevel | None = None,
    ) -> None:
        self.allowed_permissions = allowed_permissions
        self.approval_threshold = approval_threshold

    def evaluate(
        self,
        descriptor: ActionDescriptor,
        *,
        rules: Iterable[PolicyRule] = (),
        grants: Iterable[ApprovalGrant] = (),
        policy_version: str = "",
        config_version: str = "",
    ) -> PolicyEvaluation:
        applicable = [
            rule for rule in rules if self._matches_rule(rule, descriptor, policy_version)
        ]
        hard_denies = [rule for rule in applicable if rule.effect is PolicyRuleEffect.DENY]
        if hard_denies:
            selected = self._sort_rules(hard_denies)[0]
            return self._result(
                "deny",
                descriptor,
                f"denied by policy rule {selected.id}",
                applicable,
                policy_version,
                config_version,
            )
        if (
            self.allowed_permissions is not None
            and descriptor.action.value not in self.allowed_permissions
        ):
            return self._result(
                "deny",
                descriptor,
                "action permission is not allowed",
                applicable,
                policy_version,
                config_version,
            )
        active_grants = [
            grant
            for grant in grants
            if self._matches_grant(grant, descriptor, policy_version, config_version)
        ]
        if active_grants:
            selected_grant = sorted(active_grants, key=lambda item: item.id)[0]
            return PolicyEvaluation(
                decision=PolicyDecision.ALLOW,
                risk=descriptor.risk,
                reason=f"allowed by grant {selected_grant.id}",
                descriptor=descriptor,
                matched_rule_ids=tuple(rule.id for rule in self._sort_rules(applicable)),
                matched_grant_id=selected_grant.id,
                policy_version=policy_version,
                config_version=config_version,
            )
        allows = [rule for rule in applicable if rule.effect is PolicyRuleEffect.ALLOW]
        if allows:
            selected = self._sort_rules(allows)[0]
            return self._result(
                "allow",
                descriptor,
                f"allowed by policy rule {selected.id}",
                applicable,
                policy_version,
                config_version,
            )
        asks = [rule for rule in applicable if rule.effect is PolicyRuleEffect.ASK]
        if asks:
            selected = self._sort_rules(asks)[0]
            return self._result(
                "require_approval",
                descriptor,
                f"approval required by policy rule {selected.id}",
                applicable,
                policy_version,
                config_version,
            )
        if (
            self.approval_threshold is not None
            and RISK_ORDER[descriptor.risk] >= RISK_ORDER[self.approval_threshold]
        ):
            return self._result(
                "require_approval",
                descriptor,
                f"{descriptor.risk} action requires operator approval",
                applicable,
                policy_version,
                config_version,
            )
        if descriptor.side_effect:
            return self._result(
                "require_approval",
                descriptor,
                "side-effect action has no matching allow rule",
                applicable,
                policy_version,
                config_version,
            )
        return self._result(
            "allow",
            descriptor,
            "read-only action is within the default scope",
            applicable,
            policy_version,
            config_version,
        )

    @staticmethod
    def _sort_rules(rules: list[PolicyRule]) -> list[PolicyRule]:
        return sorted(
            rules,
            key=lambda rule: (
                -PolicyEngine._specificity(rule.pattern),
                -rule.priority,
                -_SOURCE_ORDER[rule.source],
                rule.id,
            ),
        )

    @staticmethod
    def _specificity(pattern: str) -> int:
        return sum(1 for char in pattern if char not in "*?")

    @staticmethod
    def _matches_rule(rule: PolicyRule, descriptor: ActionDescriptor, policy_version: str) -> bool:
        if not rule.enabled or rule.action is not descriptor.action:
            return False
        if rule.workspace_ref is not None and rule.workspace_ref != descriptor.workspace_ref:
            return False
        if rule.session_id is not None and rule.session_id != descriptor.session_id:
            return False
        if rule.policy_version and rule.policy_version != policy_version:
            return False
        if RISK_ORDER[descriptor.risk] > RISK_ORDER[rule.max_risk]:
            return False
        resources = descriptor.resources
        if rule.resource_kind is not None:
            resources = tuple(item for item in resources if item.kind is rule.resource_kind)
        return bool(resources) and all(
            PolicyEngine._matches_pattern(rule, item) for item in resources
        )

    @staticmethod
    def _matches_pattern(rule: PolicyRule, resource: ResourceSelector) -> bool:
        if resource.kind is ResourceKind.PATH:
            return fnmatchcase(resource.value, rule.pattern)
        if resource.kind is ResourceKind.COMMAND:
            return resource.value == rule.pattern or resource.value.startswith(
                rule.pattern.rstrip() + ","
            )
        return resource.value == rule.pattern

    @staticmethod
    def _matches_grant(
        grant: ApprovalGrant, descriptor: ActionDescriptor, policy_version: str, config_version: str
    ) -> bool:
        if (
            not grant.is_active()
            or grant.action is not descriptor.action
            or grant.workspace_ref != descriptor.workspace_ref
        ):
            return False
        if grant.session_id is not None and grant.session_id != descriptor.session_id:
            return False
        if grant.policy_version != policy_version or grant.config_version != config_version:
            return False
        approved = {(item.kind, item.value) for item in grant.resources}
        return all((item.kind, item.value) in approved for item in descriptor.resources)

    @staticmethod
    def _result(
        decision: str,
        descriptor: ActionDescriptor,
        reason: str,
        rules: list[PolicyRule],
        policy_version: str,
        config_version: str,
    ) -> PolicyEvaluation:
        return PolicyEvaluation(
            decision=PolicyDecision(decision),
            risk=descriptor.risk,
            reason=reason,
            descriptor=descriptor,
            matched_rule_ids=tuple(rule.id for rule in PolicyEngine._sort_rules(rules)),
            policy_version=policy_version,
            config_version=config_version,
        )


def _safe(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyNormalizationError("resource value must be a non-empty string")
    if SecretRedactor.replacement in value or "credential://" in value:
        raise PolicyNormalizationError(
            "credential or redaction marker cannot enter a selector", code="secret"
        )
    redacted = SecretRedactor().redact_text(value)
    if redacted != value:
        raise PolicyNormalizationError("secret-like value cannot enter a selector", code="secret")
    return value.strip()


def normalize_path_selector(path: str | Path, workspace: Path) -> ResourceSelector:
    raw = _safe(str(path)).replace("\\", "/")
    root = workspace.resolve(strict=True)
    try:
        candidate = (root / raw).resolve(strict=False)
        relative = candidate.relative_to(root).as_posix()
    except (OSError, ValueError) as exc:
        raise PolicyNormalizationError("path escapes workspace", code="path_escape") from exc
    if not relative or relative.startswith("../"):
        raise PolicyNormalizationError("path escapes workspace", code="path_escape")
    canonical = posixpath.normpath(relative)
    if os.name == "nt":
        canonical = canonical.casefold()
    return ResourceSelector(kind=ResourceKind.PATH, value=canonical)


_SHELL_MARKERS = ("&&", "||", ";", "|", "$(", "`", ">", "<")


def normalize_command_selector(command: Iterable[object]) -> ResourceSelector:
    if isinstance(command, (str, bytes)):
        raise PolicyNormalizationError(
            "compound command must be tokenized", code="compound_command"
        )
    tokens = tuple(_safe(str(item)) for item in command)
    if not tokens:
        raise PolicyNormalizationError("command is empty")
    if any(marker in token for token in tokens for marker in _SHELL_MARKERS):
        raise PolicyNormalizationError(
            "shell operators are not valid command selectors", code="compound_command"
        )
    executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    normalized = (executable, *tokens[1:])
    return ResourceSelector(
        kind=ResourceKind.COMMAND,
        value=json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
    )


def normalize_network_target_selector(url: str) -> ResourceSelector:
    raw = _safe(url)
    try:
        parsed: SplitResult = urlsplit(raw)
    except ValueError as exc:
        raise PolicyNormalizationError(
            "network target cannot be parsed", code="invalid_network"
        ) from exc
    if not parsed.scheme or not parsed.hostname or parsed.username or parsed.password:
        raise PolicyNormalizationError(
            "network target must include scheme/host and no credentials", code="invalid_network"
        )
    try:
        host = parsed.hostname.casefold()
        port = parsed.port
    except ValueError as exc:
        raise PolicyNormalizationError(
            "network target has an invalid port", code="invalid_network"
        ) from exc
    netloc = host if port is None else f"{host}:{port}"
    path = posixpath.normpath(parsed.path or "/")
    canonical = urlunsplit((parsed.scheme.casefold(), netloc, path, "", ""))
    return ResourceSelector(kind=ResourceKind.NETWORK_TARGET, value=canonical)


def normalize_package_selector(
    manager: str, name: str, version: str, source: str
) -> ResourceSelector:
    values = [
        _safe(item).casefold() if index < 2 else _safe(item)
        for index, item in enumerate((manager, name, version, source))
    ]
    if not values[2] or not values[3]:
        raise PolicyNormalizationError(
            "package version and source are required", code="unresolved_package"
        )
    return ResourceSelector(
        kind=ResourceKind.PACKAGE,
        value=json.dumps(
            dict(zip(("manager", "name", "version", "source"), values, strict=True)),
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def normalize_skill_selector(registry: str, skill_id: str, version: str) -> ResourceSelector:
    values = (_safe(registry).casefold(), _safe(skill_id), _safe(version))
    return ResourceSelector(
        kind=ResourceKind.SKILL,
        value=json.dumps(
            dict(zip(("registry", "id", "version"), values, strict=True)),
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def arguments_fingerprint(arguments: object) -> str:
    return _digest(arguments)


def risk_for_permission(permission: str) -> RiskLevel:
    return {"read": RiskLevel.LOW, "write": RiskLevel.MEDIUM, "execute": RiskLevel.HIGH}.get(
        permission, RiskLevel.CRITICAL
    )
