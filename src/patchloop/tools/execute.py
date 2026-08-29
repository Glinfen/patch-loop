"""Restricted local-process fallback for running repository tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import ClassVar

from pydantic import BaseModel, Field

from patchloop.domain import ErrorKind
from patchloop.sandbox import LocalProcessSandbox, SandboxError, SandboxTimeoutError
from patchloop.tools.base import (
    PermissionLevel,
    Tool,
    ToolContext,
    ToolInputModel,
    ToolTimeoutError,
)


class RunTestsInput(ToolInputModel):
    command: list[str] = Field(default_factory=lambda: ["python", "-m", "pytest", "-q"])
    timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    max_output_chars: int = Field(default=30_000, ge=1_000, le=200_000)


class RunTestsTool(Tool):
    name = "run_tests"
    description = "Run pytest or unittest in the repository with a timeout and bounded output."
    input_model = RunTestsInput
    permission = PermissionLevel.EXECUTE

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        request = RunTestsInput.model_validate(arguments)
        command = self._normalize_command(request.command, context)
        sandbox = context.sandbox or LocalProcessSandbox()
        try:
            completed = sandbox.execute(
                command,
                context.repository,
                timeout_seconds=request.timeout_seconds,
                max_output_chars=request.max_output_chars,
            )
        except SandboxTimeoutError as exc:
            raise ToolTimeoutError(
                f"test command timed out after {request.timeout_seconds:g} seconds"
            ) from exc
        except SandboxError as exc:
            raise ValueError(str(exc)) from exc
        return json.dumps(
            {
                "exit_code": completed.exit_code,
                "output": completed.output,
                "sandbox": completed.backend,
            },
            ensure_ascii=False,
        )

    def classify_output(self, output: str) -> ErrorKind | None:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            return ErrorKind.TEST_FAILURE
        if not isinstance(payload, dict) or payload.get("exit_code") != 0:
            details = payload.get("output", "") if isinstance(payload, dict) else output
            if isinstance(details, str) and (
                "SyntaxError" in details or "IndentationError" in details
            ):
                return ErrorKind.SYNTAX_ERROR
            return ErrorKind.TEST_FAILURE
        return None

    @staticmethod
    def _normalize_command(command: list[str], context: ToolContext) -> list[str]:
        if not command:
            raise ValueError("test command cannot be empty")
        executable = Path(command[0]).name.casefold()
        arguments = command[1:]
        if executable in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}:
            if arguments[:2] not in (["-m", "pytest"], ["-m", "unittest"]):
                raise ValueError("Python test command must use '-m pytest' or '-m unittest'")
            normalized = [sys.executable, *arguments]
        elif executable in {"pytest", "pytest.exe"}:
            normalized = [sys.executable, "-m", "pytest", *arguments]
        else:
            raise ValueError("only pytest and unittest test commands are allowed")
        for argument in normalized[3:]:
            candidate = argument.split("=", 1)[1] if "=" in argument else argument
            if argument.startswith("-") and candidate == argument:
                continue
            windows_path = PureWindowsPath(candidate)
            posix_path = PurePosixPath(candidate)
            if (
                windows_path.is_absolute()
                or posix_path.is_absolute()
                or ".." in windows_path.parts
                or ".." in posix_path.parts
            ):
                raise ValueError(f"test path escapes repository: {argument}")
        return normalized


class RunCommandInput(ToolInputModel):
    command: list[str] = Field(min_length=1)
    timeout_seconds: float = Field(default=30.0, gt=0, le=60)
    max_output_chars: int = Field(default=20_000, ge=1_000, le=100_000)


class RunCommandTool(Tool):
    name = "run_command"
    description = "Run one allowlisted diagnostic command: git status/diff or Python compileall."
    input_model = RunCommandInput
    permission = PermissionLevel.EXECUTE

    _allowed_commands: ClassVar[frozenset[tuple[str, ...]]] = frozenset(
        {
            ("git", "status", "--short"),
            ("git", "diff"),
            ("git", "diff", "--stat"),
            ("git", "diff", "--check"),
            ("python", "-m", "compileall", "-q", "."),
        }
    )

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        request = RunCommandInput.model_validate(arguments)
        command = self._normalize_command(request.command)
        sandbox = context.sandbox or LocalProcessSandbox()
        try:
            completed = sandbox.execute(
                command,
                context.repository,
                timeout_seconds=request.timeout_seconds,
                max_output_chars=request.max_output_chars,
            )
        except SandboxTimeoutError as exc:
            raise ToolTimeoutError(
                f"command timed out after {request.timeout_seconds:g} seconds"
            ) from exc
        except SandboxError as exc:
            raise ValueError(str(exc)) from exc
        return json.dumps(
            {
                "exit_code": completed.exit_code,
                "output": completed.output,
                "sandbox": completed.backend,
            },
            ensure_ascii=False,
        )

    def classify_output(self, output: str) -> ErrorKind | None:
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            return ErrorKind.COMMAND_FAILURE
        if not isinstance(payload, dict) or payload.get("exit_code") != 0:
            return ErrorKind.COMMAND_FAILURE
        return None

    @classmethod
    def _normalize_command(cls, command: list[str]) -> list[str]:
        executable = Path(command[0]).name.casefold()
        if executable in {
            "python",
            "python.exe",
            "python3",
            "python3.exe",
            "py",
            "py.exe",
        }:
            canonical = ("python", *command[1:])
            normalized = [sys.executable, *command[1:]]
        elif executable in {"git", "git.exe"}:
            canonical = ("git", *command[1:])
            normalized = ["git", *command[1:]]
        else:
            raise ValueError("command is not allowlisted")
        if canonical not in cls._allowed_commands:
            raise ValueError(f"command is not allowlisted: {' '.join(canonical)}")
        return normalized
