"""Fail-closed policy configuration with explicit file provenance."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from patchloop.execution.policy import PolicyRule, digest


class PolicyConfigurationError(ValueError):
    pass


class PolicyConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1.0"] = "1.0"
    enabled: bool = True
    rules: list[dict[str, Any]] = Field(default_factory=list)


class PolicyConfigurationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    fingerprint: str = ""
    rules: tuple[PolicyRule, ...] = ()

    def rules_for(self, policy_version: str) -> tuple[PolicyRule, ...]:
        return tuple(
            rule.model_copy(update={"policy_version": policy_version}) for rule in self.rules
        )


def load_policy_configuration(
    workspace: Path,
    *,
    session_id: str | None = None,
    system_path: Path | None = None,
    user_path: Path | None = None,
    project_path: Path | None = None,
    session_path: Path | None = None,
) -> PolicyConfigurationSnapshot:
    """Validate every configured rule before provider initialization or execution."""
    workspace = workspace.resolve(strict=True)
    required_paths = [
        path for path in (system_path, user_path, project_path, session_path) if path is not None
    ]
    if system_path is None and os.environ.get("PATCHLOOP_SYSTEM_POLICY"):
        system_path = Path(os.environ["PATCHLOOP_SYSTEM_POLICY"])
        required_paths.append(system_path)
    if user_path is None:
        user_path = Path(
            os.environ.get(
                "PATCHLOOP_USER_POLICY", str(Path.home() / ".config" / "patchloop" / "policy.json")
            )
        )
        if os.environ.get("PATCHLOOP_USER_POLICY"):
            required_paths.append(user_path)
    if any(not path.is_file() for path in required_paths):
        raise PolicyConfigurationError("an explicitly configured policy file is missing")
    sources = (
        ("system", system_path),
        ("user", user_path),
        ("project", project_path or workspace / ".patchloop" / "policy.json"),
        ("session", session_path),
    )
    rules: list[PolicyRule] = []
    snapshots: list[dict[str, Any]] = []
    enabled = False
    for source, path in sources:
        if path is None or not path.exists():
            continue
        try:
            config = PolicyConfiguration.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
            for entry in config.rules:
                if source == "session" and session_id is None:
                    raise ValueError("session config requires session identity")
                bound: dict[str, Any] = {
                    "source": source,
                    "workspace_ref": str(workspace),
                    "session_id": session_id if source == "session" else None,
                    "policy_version": "configuration",
                    "created_at": datetime(1970, 1, 1, tzinfo=UTC),
                    "updated_at": datetime(1970, 1, 1, tzinfo=UTC),
                    **entry,
                }
                rule = PolicyRule.model_validate(bound)
                if (
                    rule.source != source
                    or rule.workspace_ref != str(workspace)
                    or rule.session_id != (session_id if source == "session" else None)
                    or rule.policy_version != "configuration"
                ):
                    raise ValueError("configuration provenance cannot be overridden")
                rules.append(rule.model_copy(update={"id": f"{source}:{rule.id}"}))
            snapshots.append({"source": source, "configuration": config.model_dump(mode="json")})
            enabled = enabled or config.enabled or bool(config.rules)
        except (OSError, ValueError, ValidationError) as exc:
            raise PolicyConfigurationError(
                f"invalid {source} policy configuration: {path}"
            ) from exc
    if len({rule.id for rule in rules}) != len(rules):
        raise PolicyConfigurationError("policy rule IDs must be unique within each source")
    return PolicyConfigurationSnapshot(
        enabled=enabled, fingerprint=digest(snapshots) if snapshots else "", rules=tuple(rules)
    )
