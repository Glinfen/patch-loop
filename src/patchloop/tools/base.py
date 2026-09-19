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

    @abstractmethod
    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        raise NotImplementedError
