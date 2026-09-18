"""Typed tool contract and repository boundary enforcement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from patchloop.changes import FileChangeTracker
from patchloop.domain import ErrorKind, Plan
from patchloop.providers.base import ToolSpec

if TYPE_CHECKING:
    from patchloop.sandbox import CommandSandbox


class PathDeniedError(ValueError):
    pass


class ToolTimeoutError(TimeoutError):
    pass


class PermissionLevel(StrEnum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


class ToolInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolContext:
    def __init__(self, repository: Path, sandbox: CommandSandbox | None = None) -> None:
        self.repository = repository.resolve(strict=True)
        if not self.repository.is_dir():
            raise ValueError(f"repository is not a directory: {self.repository}")
        self.changes = FileChangeTracker(self.repository)
        self.plan: Plan | None = None
        self.requires_replan = False
        self.replan_count = 0
        self.recent_paths: list[str] = []
        self.sandbox = sandbox
        # Runtimes may attach a session identifier without changing the tool
        # contract.  The descriptor falls back to an empty id for old callers.
        self.session_id = ""

    def resolve_path(self, relative_path: str, *, must_exist: bool = True) -> Path:
        candidate = (self.repository / relative_path).resolve(strict=must_exist)
        try:
            candidate.relative_to(self.repository)
        except ValueError as exc:
            raise PathDeniedError(f"path escapes repository: {relative_path}") from exc
        return candidate

    def remember_access(self, path: Path) -> None:
        relative = path.relative_to(self.repository).as_posix()
        self.recent_paths = [relative, *[item for item in self.recent_paths if item != relative]][
            :20
        ]


class Tool(ABC):
    name: str
    description: str
    input_model: type[BaseModel]
    permission: PermissionLevel = PermissionLevel.READ

    def specification(self) -> ToolSpec:
        schema: dict[str, Any] = self.input_model.model_json_schema()
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=schema,
            permission=self.permission,
        )

    def classify_output(self, output: str) -> ErrorKind | None:
        return None

    def policy_descriptor(self, arguments: BaseModel, context: ToolContext) -> Any:
        """Return the stable authorization input for this tool call.

        Individual tools can override this for network, package, or Skill
        resources.  The default covers the existing file, command, and
        read-only tools and intentionally derives selectors only from typed
        arguments.
        """
        from patchloop.execution.policy import (
            ActionDescriptor,
            PolicyAction,
            ResourceSelector,
            arguments_fingerprint,
            normalize_command_selector,
            normalize_path_selector,
            risk_for_permission,
        )

        values = arguments.model_dump(mode="json")
        resources: list[ResourceSelector] = []
        for key, value in values.items():
            if isinstance(value, str) and key.casefold().endswith("path"):
                resources.append(normalize_path_selector(value, context.repository))
            elif key.casefold() == "command" and isinstance(value, list):
                resources.append(normalize_command_selector(value))
        action = {
            PermissionLevel.READ: PolicyAction.READ,
            PermissionLevel.WRITE: PolicyAction.EDIT,
            PermissionLevel.EXECUTE: PolicyAction.EXECUTE,
        }[self.permission]
        return ActionDescriptor(
            action=action,
            tool_name=self.name,
            workspace_ref=str(context.repository),
            session_id=getattr(context, "session_id", ""),
            resources=tuple(resources),
            arguments_fingerprint=arguments_fingerprint(values),
            side_effect=self.permission is not PermissionLevel.READ,
            risk=risk_for_permission(self.permission.value),
        )

    @abstractmethod
    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        raise NotImplementedError
