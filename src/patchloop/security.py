"""Risk assessment, approval records, and credential redaction."""

from __future__ import annotations

import re
from copy import deepcopy
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class UntrustedContentFinding(StrEnum):
    CREDENTIAL_REDACTED = "credential_redacted"
    PROMPT_INJECTION_BLOCKED = "prompt_injection_blocked"


RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


class ApprovalRequest(BaseModel):
    task_id: str
    call_id: str
    tool_name: str
    risk: RiskLevel
    reason: str
    arguments: dict[str, Any]


class RiskAssessment(BaseModel):
    risk: RiskLevel
    allowed: bool
    approval_required: bool = False
    decision: PolicyDecision | None = None
    reason: str

    @model_validator(mode="after")
    def project_decision(self) -> RiskAssessment:
        decision = self.decision
        if decision is None:
            decision = (
                PolicyDecision.REQUIRE_APPROVAL
                if self.approval_required and not self.allowed
                else PolicyDecision.ALLOW
                if self.allowed
                else PolicyDecision.DENY
            )
            object.__setattr__(self, "decision", decision)
        if self.allowed is not (decision is PolicyDecision.ALLOW):
            raise ValueError("Policy decision and allowed projection disagree")
        if self.approval_required is not (decision is PolicyDecision.REQUIRE_APPROVAL):
            raise ValueError("Policy decision and approval projection disagree")
        return self


class UntrustedContentInspection(BaseModel):
    safe_text: str
    findings: list[UntrustedContentFinding] = Field(default_factory=list)


class SecretRedactor:
    replacement = "[REDACTED]"
    _sensitive_names: ClassVar[set[str]] = {
        "apikey",
        "accesstoken",
        "authtoken",
        "authorization",
        "password",
        "passwd",
        "secret",
        "token",
    }

    _patterns = (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
        re.compile(
            r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)"
            r"\b\s*[=:]\s*)([\"']?)([^\s,;\"'}]+)([\"']?)"
        ),
    )

    def redact_text(self, value: str) -> str:
        value = re.sub(
            r"(?i)([a-z][a-z0-9+.-]*://)[^/\s@]+@",
            lambda match: match.group(1) + self.replacement + "@",
            value,
        )
        redacted = self._patterns[0].sub("Bearer " + self.replacement, value)
        redacted = self._patterns[1].sub(self.replacement, redacted)
        return self._patterns[2].sub(
            lambda match: f"{match.group(1)}{match.group(2)}{self.replacement}{match.group(4)}",
            redacted,
        )

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {
                key: (
                    self.replacement
                    if re.sub(r"[^a-z]", "", str(key).casefold()) in self._sensitive_names
                    and not _is_credential_marker(item)
                    else self.redact(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact(item) for item in value)
        return value


class UnresolvedToolArgument(ValueError):
    """Raised when persisted placeholders reach an execution boundary."""


class CredentialBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(pattern=r"^/(?:[^/~]|~[01])+(?:/(?:[^/~]|~[01])+)*$")
    reference: str = Field(pattern=r"^credential://[A-Za-z0-9._:/-]+$")


class CredentialResolver(Protocol):
    def __call__(self, reference: str) -> str: ...


class PersistedToolArguments(BaseModel):
    """Secret-safe tool arguments; references are resolved only in memory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arguments: dict[str, Any]
    credential_bindings: list[CredentialBinding] = Field(default_factory=list)
    redacted_paths: list[str] = Field(default_factory=list)

    def materialize(self, resolver: CredentialResolver) -> dict[str, Any]:
        value = deepcopy(self.arguments)
        for binding in self.credential_bindings:
            _set_json_pointer(value, binding.path, resolver(binding.reference))
        assert_executable_tool_arguments(value)
        return value


def persist_tool_arguments(
    arguments: dict[str, Any],
    *,
    credential_bindings: list[CredentialBinding] | None = None,
    redactor: SecretRedactor | None = None,
) -> PersistedToolArguments:
    """Replace declared secrets with references and redact every other secret."""

    bindings = credential_bindings or []
    safe = (redactor or SecretRedactor()).redact(deepcopy(arguments))
    for binding in bindings:
        _set_json_pointer(safe, binding.path, {"$credential_ref": binding.reference})
    redacted_paths: list[str] = []
    _collect_redacted_paths(safe, "", redacted_paths)
    return PersistedToolArguments(
        arguments=safe,
        credential_bindings=bindings,
        redacted_paths=redacted_paths,
    )


def assert_executable_tool_arguments(arguments: object) -> None:
    """Reject redaction/reference markers before validation or tool dispatch."""

    if isinstance(arguments, str):
        if SecretRedactor.replacement in arguments:
            raise UnresolvedToolArgument("redacted tool arguments cannot be executed")
        return
    if isinstance(arguments, dict):
        if "$credential_ref" in arguments:
            raise UnresolvedToolArgument("credential reference must be resolved before execution")
        for value in arguments.values():
            assert_executable_tool_arguments(value)
        return
    if isinstance(arguments, (list, tuple)):
        for value in arguments:
            assert_executable_tool_arguments(value)


def _set_json_pointer(value: dict[str, Any], pointer: str, replacement: object) -> None:
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    current: Any = value
    for part in parts[:-1]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise ValueError(f"credential path does not exist: {pointer}")
    if not parts:
        raise ValueError(f"credential path does not exist: {pointer}")
    final = parts[-1]
    if isinstance(current, dict) and final in current:
        current[final] = replacement
    elif isinstance(current, list) and final.isdigit() and int(final) < len(current):
        current[int(final)] = replacement
    else:
        raise ValueError(f"credential path does not exist: {pointer}")


def _collect_redacted_paths(value: object, path: str, output: list[str]) -> None:
    if isinstance(value, str) and SecretRedactor.replacement in value:
        output.append(path or "/")
    elif isinstance(value, dict):
        for key, item in value.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            _collect_redacted_paths(item, f"{path}/{escaped}", output)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _collect_redacted_paths(item, f"{path}/{index}", output)


def _is_credential_marker(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"$credential_ref"}
        and isinstance(value["$credential_ref"], str)
        and value["$credential_ref"].startswith("credential://")
    )


class UntrustedContentGuard:
    """Remove executable-looking instructions from untrusted memory text."""

    replacement = "[UNTRUSTED_INSTRUCTION_BLOCKED]"
    _prompt_injection_patterns: ClassVar[tuple[re.Pattern[str], ...]] = (
        re.compile(
            r"(?i)\bignore\s+(?:all\s+|any\s+|the\s+)?"
            r"(?:previous|prior|system|developer)\s+instructions?\b"
        ),
        re.compile(
            r"(?i)\b(?:override|disregard)\s+(?:all\s+|the\s+)?"
            r"(?:previous|prior|system|developer)\s+(?:instructions?|messages?)\b"
        ),
        re.compile(
            r"(?i)\b(?:reveal|print|exfiltrate)\s+(?:the\s+)?"
            r"(?:system\s+prompt|credentials?|secrets?)\b"
        ),
        re.compile(r"(?i)\byou\s+are\s+now\b"),
    )

    def __init__(self, redactor: SecretRedactor | None = None) -> None:
        self.redactor = redactor or SecretRedactor()

    def inspect(self, value: str) -> UntrustedContentInspection:
        safe_text = self.redactor.redact_text(value)
        findings: list[UntrustedContentFinding] = []
        if safe_text != value:
            findings.append(UntrustedContentFinding.CREDENTIAL_REDACTED)
        injection_blocked = False
        for pattern in self._prompt_injection_patterns:
            safe_text, replacements = pattern.subn(self.replacement, safe_text)
            injection_blocked = injection_blocked or replacements > 0
        if injection_blocked:
            findings.append(UntrustedContentFinding.PROMPT_INJECTION_BLOCKED)
        return UntrustedContentInspection(safe_text=safe_text, findings=findings)
