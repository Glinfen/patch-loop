"""Deterministic, serializable policy inputs and exact authorization scopes."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.security import RISK_ORDER, PolicyDecision, RiskLevel, SecretRedactor

if TYPE_CHECKING:
    from patchloop.tools.base import ToolContext


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _path_matches(path: list[str], pattern: list[str]) -> bool:
    if not pattern:
        return not path
    if pattern[0] == "**":
        return any(_path_matches(path[index:], pattern[1:]) for index in range(len(path) + 1))
    return bool(
        path and fnmatch.fnmatchcase(path[0], pattern[0]) and _path_matches(path[1:], pattern[1:])
    )


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


class ApprovalScopeKind(StrEnum):
    ONCE = "once"
    SESSION = "session"
    RESOURCE = "resource"


class PolicyRuleEffect(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PolicyNormalizationError(ValueError):
    """A safe reason that can be reported without reproducing unsafe input."""


def _safe(value: str) -> str:
    if (
        not value
        or any(ord(char) < 32 for char in value)
        or SecretRedactor().redact_text(value) != value
        or re.search(r"(?i)(credential|redacted|password|secret|token[=:])", value)
    ):
        raise PolicyNormalizationError("resource contains credentials or invalid characters")
    return value


class ResourceSelector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ResourceKind
    value: str
    digest: str = ""

    @model_validator(mode="after")
    def validate_digest(self) -> ResourceSelector:
        _safe(self.value)
        if self.kind is ResourceKind.PATH and (
            self.value.startswith(("/", "\\"))
            or "\\" in self.value
            or ":" in self.value
            or ".." in self.value.split("/")
        ):
            raise ValueError("path selector must be repository-relative POSIX")
        if self.kind is ResourceKind.PATH:
            parts = self.value.casefold().split("/")
            if (
                any(part in {".git", ".patchloop", ".ssh"} for part in parts)
                or parts[-1] in {".npmrc", ".pypirc", "id_rsa", "id_ed25519", ".env"}
                or parts[-1].startswith(".env.")
            ):
                raise PolicyNormalizationError("credential or control-plane path is denied")
        expected = digest({"kind": self.kind.value, "value": self.value})
        if self.digest and self.digest != expected:
            raise ValueError("resource digest mismatch")
        object.__setattr__(self, "digest", expected)
        return self


def normalize_path(value: str, context: ToolContext) -> ResourceSelector:
    _safe(value)
    try:
        path = context.resolve_path(value.replace("\\", "/"), must_exist=False)
        canonical = path.relative_to(context.repository).as_posix()
    except (OSError, ValueError) as exc:
        raise PolicyNormalizationError("path escapes repository or cannot be resolved") from exc
    if os.name == "nt":
        canonical = canonical.casefold()
    parts = canonical.casefold().split("/")
    if (
        any(part in {".git", ".patchloop", ".ssh"} for part in parts)
        or parts[-1] in {".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials.json"}
        or parts[-1] == ".env"
        or parts[-1].startswith(".env.")
    ):
        raise PolicyNormalizationError("credential or persistent control-plane resource is denied")
    return ResourceSelector(kind=ResourceKind.PATH, value=canonical)


def normalize_command(argv: list[str]) -> ResourceSelector:
    if not argv:
        raise PolicyNormalizationError("command is empty")
    for token in argv:
        _safe(token)
        if any(marker in token for marker in (";", "|", "&", "<", ">", "`", "$(", "\n")):
            raise PolicyNormalizationError("compound command or redirection is denied")
    executable = argv[0].replace("\\", "/")
    name = executable.rsplit("/", 1)[-1].casefold().removesuffix(".exe")
    if name in {
        "sh",
        "bash",
        "cmd",
        "powershell",
        "pwsh",
        "curl",
        "wget",
        "ssh",
        "nc",
        "pip",
        "pip3",
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "uv",
        "poetry",
        "conda",
        "sudo",
        "doas",
        "env",
        "xargs",
        "nohup",
        "timeout",
    }:
        raise PolicyNormalizationError("implicit shell or network command is denied")
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|py|node|ruby|perl", name):
        # Short options can carry their value or follow other switches in a
        # cluster. Long options can carry a value after '='. Treat these just
        # like the separated form; an allow rule must not override this deny.
        inline_option = {
            "node": r"^-[ip]*[ep]",
            "ruby": r"^-[anlpw]*e",
            "perl": r"^-[anlpw]*[eE]",
        }.get(name, r"^-[bBdEiIOPqRsSuvVx]*c")
        if any(
            token.startswith(("-c", "-e"))
            or token.split("=", 1)[0] == "--eval"
            or (name == "node" and token.split("=", 1)[0] == "--print")
            or re.match(inline_option, token)
            for token in argv[1:]
        ):
            raise PolicyNormalizationError("inline interpreter command is denied")
    if (
        name.startswith("python")
        and "-m" in argv
        and any(token in {"pip", "pip3", "ensurepip", "uv"} for token in argv[1:])
    ):
        raise PolicyNormalizationError("dependency installation requires a dedicated action")
    if name == "git" and any(
        token in {"push", "config", "clone", "fetch", "pull", "remote", "submodule"}
        for token in argv[1:]
    ):
        raise PolicyNormalizationError("persistent or publishing git command is denied")
    return ResourceSelector(
        kind=ResourceKind.COMMAND, value=canonical_json([executable, *argv[1:]])
    )


def normalize_network_target(value: str) -> ResourceSelector:
    _safe(value)
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("invalid network target")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("network credentials are denied")
        host = parsed.hostname.encode("idna").decode().casefold().rstrip(".")
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if port == 0:
            raise ValueError("invalid port")
        if not host or any(c in host for c in " *\\/"):
            raise ValueError("invalid host")
        path = parsed.path or "/"
        return ResourceSelector(
            kind=ResourceKind.NETWORK_TARGET,
            value=canonical_json(
                {
                    "scheme": parsed.scheme,
                    "host": host,
                    "port": port,
                    "path": path,
                }
            ),
        )
    except (ValueError, UnicodeError) as exc:
        raise PolicyNormalizationError("network target is invalid or contains credentials") from exc


def normalize_package(manager: str, name: str, version: str, source: str) -> ResourceSelector:
    values = [manager.casefold(), name.casefold(), version, source]
    for value in values:
        _safe(value)
    if "://" in source:
        parsed = urlsplit(source)
        if parsed.username is not None or parsed.password is not None:
            raise PolicyNormalizationError("package source credentials are denied")
    return ResourceSelector(kind=ResourceKind.PACKAGE, value=canonical_json(values))


def normalize_skill(registry: str, skill_id: str, version: str) -> ResourceSelector:
    values = [registry, skill_id, version]
    for value in values:
        _safe(value)
    if "://" in registry:
        parsed = urlsplit(registry)
        if parsed.username is not None or parsed.password is not None:
            raise PolicyNormalizationError("skill source credentials are denied")
    return ResourceSelector(kind=ResourceKind.SKILL, value=canonical_json(values))


class ActionDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: PolicyAction
    tool_name: str
    workspace_ref: str
    session_id: str
    resources: tuple[ResourceSelector, ...]
    arguments_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    resource_state_fingerprint: str = ""
    side_effect: bool
    risk: RiskLevel

    @model_validator(mode="after")
    def canonical_resources(self) -> ActionDescriptor:
        if not self.resources:
            raise ValueError("an action must declare resources")
        ordered = tuple(sorted(set(self.resources), key=lambda item: (item.kind, item.value)))
        object.__setattr__(self, "resources", ordered)
        return self

    @property
    def digest(self) -> str:
        return digest(self.model_dump(mode="json"))


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    schema_version: Literal["1.0"] = "1.0"
    source: Literal["builtin", "system", "user", "project", "session"]
    workspace_ref: str = Field(min_length=1)
    session_id: str | None = None
    action: PolicyAction
    resource_kind: ResourceKind
    pattern: str
    effect: PolicyRuleEffect
    max_risk: RiskLevel = RiskLevel.HIGH
    priority: int = 0
    enabled: bool = True
    policy_version: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_pattern(self) -> PolicyRule:
        _safe(self.pattern)
        if self.source == "session" and not self.session_id:
            raise ValueError("session rules require a session binding")
        if self.resource_kind is ResourceKind.PATH:
            if (
                self.pattern.startswith("/")
                or "\\" in self.pattern
                or ":" in self.pattern
                or ".." in self.pattern.split("/")
            ):
                raise ValueError("path patterns must be repository-relative POSIX")
        elif self.resource_kind is ResourceKind.COMMAND:
            prefix = json.loads(self.pattern)
            if (
                not isinstance(prefix, list)
                or not prefix
                or not all(isinstance(item, str) and item for item in prefix)
            ):
                raise ValueError("command rules require a nonempty token prefix")
        elif self.resource_kind in {ResourceKind.PACKAGE, ResourceKind.SKILL}:
            parts = json.loads(self.pattern)
            expected = 4 if self.resource_kind is ResourceKind.PACKAGE else 3
            if (
                not isinstance(parts, list)
                or len(parts) != expected
                or not all(isinstance(part, str) and part for part in parts)
            ):
                raise ValueError("package/skill rule requires an exact versioned selector")
        elif self.resource_kind is ResourceKind.NETWORK_TARGET:
            target = json.loads(self.pattern)
            if (
                not isinstance(target, dict)
                or set(target) != {"host", "port", "path", "scheme"}
                or target["scheme"] not in {"http", "https"}
                or not isinstance(target["host"], str)
                or not isinstance(target["port"], int)
                or not 0 < target["port"] < 65536
            ):
                raise ValueError("network rules require an exact normalized target")
        return self

    def matches(
        self, descriptor: ActionDescriptor, resource: ResourceSelector, version: str
    ) -> bool:
        if not (
            self.enabled
            and self.policy_version == version
            and self.workspace_ref == descriptor.workspace_ref
            and self.session_id in {None, descriptor.session_id}
            and self.action == descriptor.action
            and self.resource_kind == resource.kind
            and (
                self.effect == PolicyRuleEffect.DENY
                or RISK_ORDER[descriptor.risk] <= RISK_ORDER[self.max_risk]
            )
        ):
            return False
        if resource.kind == ResourceKind.PATH:
            value, pattern = resource.value, self.pattern
            if os.name == "nt":
                value, pattern = value.casefold(), pattern.casefold()
            return _path_matches(value.split("/"), pattern.split("/"))
        if resource.kind == ResourceKind.COMMAND:
            try:
                prefix = json.loads(self.pattern)
                argv = json.loads(resource.value)
                return bool(isinstance(prefix, list) and prefix and argv[: len(prefix)] == prefix)
            except (ValueError, TypeError):
                return False
        if resource.kind == ResourceKind.NETWORK_TARGET:
            target = json.loads(resource.value)
            pattern = json.loads(self.pattern)
            host = pattern["host"]
            host_matches = target["host"] == host
            if host.startswith("*."):
                host_matches = target["host"].endswith(host[1:]) and target["host"] != host[2:]
            prefix = pattern["path"].rstrip("/")
            return bool(
                host_matches
                and target["port"] == pattern["port"]
                and target["scheme"] == pattern["scheme"]
                and (target["path"] == prefix or target["path"].startswith(prefix + "/"))
            )
        return resource.value == self.pattern


class ApprovalGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    schema_version: Literal["1.0"] = "1.0"
    source_approval_id: str
    scope_kind: ApprovalScopeKind
    session_id: str | None = None
    workspace_ref: str
    action: PolicyAction
    resources: tuple[ResourceSelector, ...]
    arguments_fingerprint: str
    resource_state_fingerprint: str = ""
    rules_fingerprint: str = ""
    tool_name: str
    policy_version: str
    config_version: str
    expires_at: datetime | None = None
    remaining_uses: int | None = Field(default=None, ge=0)
    status: Literal["active", "revoked", "expired", "exhausted"] = "active"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    revoked_reason: str | None = None
    version: int = Field(default=1, ge=1)

    def expire(self) -> ApprovalGrant:
        if (
            self.status == "active"
            and self.expires_at is not None
            and self.expires_at <= datetime.now(UTC)
        ):
            return self.model_copy(update={"status": "expired", "version": self.version + 1})
        return self

    def consume(
        self, descriptor: ActionDescriptor, *, policy_version: str, config_version: str
    ) -> ApprovalGrant:
        if not self.matches(descriptor, policy_version, config_version):
            from patchloop.persistence_contracts import ApprovalConflict

            raise ApprovalConflict(self.id, "grant_binding_mismatch", "consumed")
        remaining = None if self.remaining_uses is None else self.remaining_uses - 1
        return self.model_copy(
            update={
                "remaining_uses": remaining,
                "status": "exhausted" if remaining == 0 else "active",
                "version": self.version + 1,
            }
        )

    @model_validator(mode="after")
    def validate_scope(self) -> ApprovalGrant:
        if self.scope_kind == ApprovalScopeKind.ONCE:
            raise ValueError("one-time requests cannot be reusable grants")
        if self.scope_kind == ApprovalScopeKind.SESSION and not self.session_id:
            raise ValueError("session grant requires a session")
        if self.scope_kind == ApprovalScopeKind.RESOURCE and self.expires_at is None:
            raise ValueError("resource grant requires expiry")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("expiry requires a timezone")
        return self

    def matches(
        self, descriptor: ActionDescriptor, policy_version: str, config_version: str
    ) -> bool:
        return (
            self.status == "active"
            and self.remaining_uses != 0
            and (self.expires_at is None or self.expires_at > datetime.now(UTC))
            and self.workspace_ref == descriptor.workspace_ref
            and (
                self.scope_kind == ApprovalScopeKind.RESOURCE
                or self.session_id == descriptor.session_id
            )
            and self.action == descriptor.action
            and self.tool_name == descriptor.tool_name
            and self.arguments_fingerprint == descriptor.arguments_fingerprint
            and self.resource_state_fingerprint == descriptor.resource_state_fingerprint
            and set(self.resources) == set(descriptor.resources)
            and self.policy_version == policy_version
            and self.config_version == config_version
        )


class PolicyEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: PolicyDecision
    risk: RiskLevel
    reason: str
    descriptor: ActionDescriptor
    matched_rule_ids: tuple[str, ...] = ()
    matched_grant_id: str | None = None
    policy_version: str
    config_version: str
    rules_fingerprint: str = ""


class PolicyEngine:
    def evaluate(
        self,
        descriptor: ActionDescriptor,
        *,
        rules: tuple[PolicyRule, ...] = (),
        grants: tuple[ApprovalGrant, ...] = (),
        policy_version: str,
        config_version: str,
    ) -> PolicyEvaluation:
        hard_reason = self._hard_denial(descriptor)
        applicable_rules = tuple(
            sorted(
                (
                    rule
                    for rule in rules
                    if rule.workspace_ref == descriptor.workspace_ref
                    and rule.session_id in {None, descriptor.session_id}
                ),
                key=lambda rule: rule.id,
            )
        )
        rules_fingerprint = (
            digest([rule.model_dump(mode="json") for rule in applicable_rules])
            if applicable_rules
            else ""
        )
        matches = [
            [rule for rule in rules if rule.matches(descriptor, resource, policy_version)]
            for resource in descriptor.resources
        ]
        deny = sorted(
            {rule.id for group in matches for rule in group if rule.effect == PolicyRuleEffect.DENY}
        )
        grant = next(
            (
                grant
                for grant in sorted(grants, key=lambda grant: grant.id)
                if grant.matches(descriptor, policy_version, config_version)
                and grant.rules_fingerprint == rules_fingerprint
            ),
            None,
        )
        selected: list[str] = []
        decision = PolicyDecision.ALLOW
        if deny or hard_reason is not None or descriptor.risk == RiskLevel.CRITICAL:
            decision = PolicyDecision.DENY
            selected = deny
            grant = None
        elif grant is None:
            sources = {
                name: rank
                for rank, name in enumerate(("builtin", "system", "user", "project", "session"))
            }
            for group in matches:
                ordered = sorted(
                    group,
                    key=lambda rule: (
                        -len(re.sub(r"[*?\[\]]", "", rule.pattern)),
                        -rule.priority,
                        -sources[rule.source],
                        rule.id,
                    ),
                )
                if ordered:
                    selected.append(ordered[0].id)
                    if ordered[0].effect == PolicyRuleEffect.ASK:
                        decision = PolicyDecision.REQUIRE_APPROVAL
                elif descriptor.side_effect or descriptor.action != PolicyAction.READ:
                    decision = PolicyDecision.REQUIRE_APPROVAL
        return PolicyEvaluation(
            decision=decision,
            risk=descriptor.risk,
            reason=hard_reason or f"policy decision: {decision.value}",
            descriptor=descriptor,
            matched_rule_ids=tuple(sorted(set(selected))),
            matched_grant_id=None if grant is None else grant.id,
            policy_version=policy_version,
            config_version=config_version,
            rules_fingerprint=rules_fingerprint,
        )

    @staticmethod
    def _hard_denial(descriptor: ActionDescriptor) -> str | None:
        external = {ResourceKind.NETWORK_TARGET, ResourceKind.PACKAGE, ResourceKind.SKILL}
        kinds = {resource.kind for resource in descriptor.resources}
        if (
            descriptor.action in {PolicyAction.READ, PolicyAction.EDIT, PolicyAction.EXECUTE}
            and kinds & external
        ):
            return "external resources require a dedicated policy action"
        required = {
            PolicyAction.NETWORK: ResourceKind.NETWORK_TARGET,
            PolicyAction.DEPENDENCY_INSTALL: ResourceKind.PACKAGE,
            PolicyAction.SKILL_LOAD: ResourceKind.SKILL,
            PolicyAction.SKILL_EXECUTE: ResourceKind.SKILL,
        }.get(descriptor.action)
        if required is not None and required not in kinds:
            return "external action is missing its declared resource"
        for resource in descriptor.resources:
            if resource.kind in external:
                try:
                    value = json.loads(resource.value)
                    if resource.kind is ResourceKind.NETWORK_TARGET:
                        if not isinstance(value, dict) or set(value) != {
                            "scheme",
                            "host",
                            "port",
                            "path",
                        }:
                            return "invalid network selector"
                        host = value["host"]
                        if not isinstance(host, str):
                            return "invalid network host"
                        host = f"[{host}]" if ":" in host else host
                        normalized = normalize_network_target(
                            f"{value['scheme']}://{host}:{value['port']}{value['path']}"
                        )
                    else:
                        length = 4 if resource.kind is ResourceKind.PACKAGE else 3
                        if (
                            not isinstance(value, list)
                            or len(value) != length
                            or not all(isinstance(item, str) for item in value)
                        ):
                            return "invalid package or skill selector"
                        normalized = (
                            normalize_package(*value)
                            if resource.kind is ResourceKind.PACKAGE
                            else normalize_skill(*value)
                        )
                    if normalized != resource:
                        return "external selector is not canonical"
                except (ValueError, TypeError):
                    return "unsafe external selector"
            if resource.kind is ResourceKind.COMMAND:
                try:
                    argv = json.loads(resource.value)
                    if not isinstance(argv, list) or not all(
                        isinstance(token, str) for token in argv
                    ):
                        return "command selector is not an argument vector"
                    if normalize_command(argv) != resource:
                        return "command selector is not canonical"
                except (ValueError, TypeError):
                    return "unsafe command selector"
        return None
