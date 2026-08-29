"""Tool registration, validation, execution, timing, and error normalization."""

from __future__ import annotations

from time import perf_counter

from pydantic import ValidationError

from patchloop.domain import ErrorKind, ToolCall, ToolResult
from patchloop.events import Event, EventLogger
from patchloop.providers.base import ToolSpec
from patchloop.tools.base import (
    PathDeniedError,
    PermissionLevel,
    Tool,
    ToolContext,
    ToolTimeoutError,
)


class ToolPolicy:
    def __init__(
        self,
        allowed_permissions: frozenset[PermissionLevel] | None = None,
        require_plan_for_mutations: bool = True,
    ) -> None:
        self.allowed_permissions = (
            frozenset({PermissionLevel.READ})
            if allowed_permissions is None
            else allowed_permissions
        )
        self.require_plan_for_mutations = require_plan_for_mutations

    def allows(self, tool: Tool) -> bool:
        return tool.permission in self.allowed_permissions


class ToolGateway:
    def __init__(
        self,
        context: ToolContext,
        tools: list[Tool],
        event_logger: EventLogger | None = None,
        policy: ToolPolicy | None = None,
    ) -> None:
        self.context = context
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("tool names must be unique")
        self.event_logger = event_logger
        self.policy = policy or ToolPolicy()
        self.history: list[ToolResult] = []

    def specifications(self) -> list[ToolSpec]:
        return [tool.specification() for tool in self._tools.values()]

    def execute(self, task_id: str, call: ToolCall) -> ToolResult:
        started = perf_counter()
        tool = self._tools.get(call.name)
        if tool is None:
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error_kind=ErrorKind.UNKNOWN_TOOL,
                output=f"unknown tool: {call.name}",
            )
            return self._finish(task_id, call, result, started)
        if not self.policy.allows(tool):
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error_kind=ErrorKind.PERMISSION_DENIED,
                output=f"permission denied for {tool.permission} tool: {call.name}",
            )
            return self._finish(task_id, call, result, started)
        if (
            self.policy.require_plan_for_mutations
            and tool.permission in {PermissionLevel.WRITE, PermissionLevel.EXECUTE}
            and self.context.plan is None
        ):
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error_kind=ErrorKind.PERMISSION_DENIED,
                output=f"an execution plan is required before using {call.name}",
            )
            return self._finish(task_id, call, result, started)
        if call.arguments_error is not None:
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error_kind=ErrorKind.INVALID_ARGUMENTS,
                output=call.arguments_error,
            )
            return self._finish(task_id, call, result, started)
        try:
            arguments = tool.input_model.model_validate(call.arguments)
            output = tool.run(arguments, self.context)
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=True,
                output=output,
            )
        except ValidationError as exc:
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error_kind=ErrorKind.INVALID_ARGUMENTS,
                output=str(exc),
            )
        except PathDeniedError as exc:
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error_kind=ErrorKind.PATH_DENIED,
                output=str(exc),
            )
        except ToolTimeoutError as exc:
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error_kind=ErrorKind.TIMEOUT,
                output=str(exc),
            )
        except (OSError, ValueError) as exc:
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error_kind=ErrorKind.EXECUTION_ERROR,
                output=str(exc),
            )
        return self._finish(task_id, call, result, started)

    def _finish(
        self,
        task_id: str,
        call: ToolCall,
        result: ToolResult,
        started: float,
    ) -> ToolResult:
        result.duration_ms = (perf_counter() - started) * 1_000
        self.history.append(result)
        if self.event_logger is not None:
            self.event_logger.emit(
                Event(
                    type="tool.completed",
                    task_id=task_id,
                    data={
                        "call": call.model_dump(mode="json"),
                        "result": result.model_dump(mode="json"),
                    },
                )
            )
        return result
