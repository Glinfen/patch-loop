"""Risk assessment, approval records, and credential redaction."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, Field


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


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
    reason: str


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
                    else self.redact(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact(item) for item in value)
        return value


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
