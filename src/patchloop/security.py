"""Risk assessment, approval records, and credential redaction."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


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
