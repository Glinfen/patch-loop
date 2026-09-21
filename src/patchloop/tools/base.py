"""Typed tool contract and repository boundary enforcement."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from contextvars import ContextVar
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from patchloop.changes import FileChangeTracker
from patchloop.domain import ErrorKind, Plan
from patchloop.execution.policy import (
    ActionDescriptor,
    PolicyAction,
    PolicyNormalizationError,
    ResourceKind,
    ResourceSelector,
    digest,
    normalize_command,
    normalize_path,
)
from patchloop.providers.base import ToolSpec
from patchloop.security import RiskLevel

if TYPE_CHECKING:
    from patchloop.sandbox import CommandSandbox
    from patchloop.workspace.models import WorkspaceHandle
    from patchloop.workspace.ownership import ChangeOwnershipLedger


_invocation: ContextVar[tuple[int, int] | None] = ContextVar("policy_checked_tool", default=None)


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
    def __init__(
        self,
        repository: Path,
        sandbox: CommandSandbox | None = None,
        *,
        workspace: WorkspaceHandle | None = None,
        ledger: ChangeOwnershipLedger | None = None,
    ) -> None:
        self.repository = repository.resolve(strict=True)
        if not self.repository.is_dir():
            raise ValueError(f"repository is not a directory: {self.repository}")
        self.changes = FileChangeTracker(self.repository)
        self.plan: Plan | None = None
        self.requires_replan = False
        self.replan_count = 0
        self.recent_paths: list[str] = []
        self.sandbox = sandbox
        self.session_id = ""
        self.config_version = "1"
        self.workspace = workspace
        self.ledger = ledger
        self.effect_id: str | None = None
        if workspace is not None and workspace.effective_root.resolve() != self.repository:
            raise ValueError("workspace does not match tool repository")

    def _run_policy_checked(self, tool: Tool, arguments: BaseModel) -> str:
        """Gateway-only dispatch; this is an API guard, not Python process isolation."""
        token = _invocation.set((id(tool), id(self)))
        try:
            return tool.run(arguments, self)
        finally:
            _invocation.reset(token)

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

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        implementation = cls.__dict__.get("run")
        if implementation is None:
            return

        @wraps(implementation)
        def guarded(self: Tool, arguments: BaseModel, context: ToolContext) -> str:
            if _invocation.get() != (id(self), id(context)):
                raise PermissionError("tools must execute through the policy gateway")
            # Do not let an adapter invoke another tool with its own authorization.
            _invocation.set(None)
            result: str = implementation(self, arguments, context)
            return result

        cls.run = guarded  # type: ignore[method-assign]

    def policy_descriptor(self, arguments: BaseModel, context: ToolContext) -> ActionDescriptor:
        """Describe repository tools; external tools must override this contract."""
        values = arguments.model_dump(mode="json")
        resources: list[ResourceSelector] = []
        file_states: list[dict[str, object]] = []
        for name, value in values.items():
            if name.casefold().endswith("path") and isinstance(value, str):
                resource = normalize_path(value, context)
                resources.append(resource)
                if self.permission is PermissionLevel.WRITE:
                    path = context.resolve_path(value, must_exist=False)
                    file_states.append(
                        {
                            "path": resource.value,
                            "existed": path.exists(),
                            "sha256": hashlib.sha256(
                                path.read_text(encoding="utf-8").encode()
                            ).hexdigest()
                            if path.exists()
                            else None,
                        }
                    )
        command = values.get("command")
        if command is not None:
            if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
                raise PolicyNormalizationError("command must be an argument vector")
            resources.append(normalize_command(command))
        if not resources:
            resources.append(ResourceSelector(kind=ResourceKind.WORKSPACE, value="."))
        action, risk = {
            PermissionLevel.READ: (PolicyAction.READ, RiskLevel.LOW),
            PermissionLevel.WRITE: (PolicyAction.EDIT, RiskLevel.MEDIUM),
            PermissionLevel.EXECUTE: (PolicyAction.EXECUTE, RiskLevel.HIGH),
        }[self.permission]
        return ActionDescriptor(
            action=action,
            tool_name=self.name,
            workspace_ref=str(context.repository),
            session_id=context.session_id,
            resources=tuple(resources),
            arguments_fingerprint=digest(values),
            resource_state_fingerprint=digest(file_states) if file_states else "",
            side_effect=self.permission != PermissionLevel.READ,
            risk=risk,
        )

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

    @abstractmethod
    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        raise NotImplementedError
