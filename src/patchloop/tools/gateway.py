"""Tool registration, validation, execution, timing, and error normalization."""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter

from pydantic import ValidationError

from patchloop.domain import ErrorKind, ToolCall, ToolResult
from patchloop.events import Event, EventLogger
from patchloop.providers.base import ToolSpec
from patchloop.security import (
    RISK_ORDER,
    ApprovalRequest,
    RiskAssessment,
    RiskLevel,
)
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
        approval_threshold: RiskLevel | None = None,
        approval_handler: Callable[[ApprovalRequest], bool] | None = None,
    ) -> None:
        self.allowed_permissions = (
            frozenset({PermissionLevel.READ})
            if allowed_permissions is None
            else allowed_permissions
        )
        self.require_plan_for_mutations = require_plan_for_mutations
        self.approval_threshold = approval_threshold
        self.approval_handler = approval_handler

    def allows(self, tool: Tool) -> bool:
        return tool.permission in self.allowed_permissions

    def assess(
        self,
        task_id: str,
        call: ToolCall,
        tool: Tool,
        context: ToolContext,
    ) -> RiskAssessment:
        risk = {
            PermissionLevel.READ: RiskLevel.LOW,
            PermissionLevel.WRITE: RiskLevel.MEDIUM,
            PermissionLevel.EXECUTE: RiskLevel.HIGH,
        }[tool.permission]
        command = call.arguments.get("command")
        if isinstance(command, list) and self._is_dangerous_command(command):
            return RiskAssessment(
                risk=RiskLevel.CRITICAL,
                allowed=False,
                reason="dangerous or network-capable command syntax is denied",
            )
        for name, value in call.arguments.items():
            if name.casefold().endswith("path") and isinstance(value, str):
                try:
                    context.resolve_path(value, must_exist=False)
                except (OSError, PathDeniedError):
                    return RiskAssessment(
                        risk=RiskLevel.CRITICAL,
                        allowed=False,
                        reason=f"path escapes repository: {value}",
                    )
        approval_required = (
            self.approval_threshold is not None
            and RISK_ORDER[risk] >= RISK_ORDER[self.approval_threshold]
        )
        if not approval_required:
            return RiskAssessment(
                risk=risk,
                allowed=True,
                reason=f"{tool.permission} action is within the authorized scope",
            )
        request = ApprovalRequest(
            task_id=task_id,
            call_id=call.id,
            tool_name=tool.name,
            risk=risk,
            reason=f"{risk} action requires operator approval",
            arguments=call.arguments,
        )
        approved = self.approval_handler(request) if self.approval_handler is not None else False
        return RiskAssessment(
            risk=risk,
            allowed=approved,
            approval_required=True,
            reason="operator approved action" if approved else "operator approval was not granted",
        )

    @staticmethod
    def _is_dangerous_command(command: list[object]) -> bool:
        values = [str(item).casefold() for item in command]
        if not values:
            return True
        executable = values[0].replace("\\", "/").rsplit("/", 1)[-1]
        if executable.removesuffix(".exe") in {
            "bash",
            "cmd",
            "curl",
            "nc",
            "netcat",
            "powershell",
            "pwsh",
            "sh",
            "ssh",
            "wget",
        }:
            return True
        return any(
            marker in value for value in values for marker in ("&&", "||", ";", "|", "$(", "`")
        )


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
        if call.arguments_error is not None:
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error_kind=ErrorKind.INVALID_ARGUMENTS,
                output=call.arguments_error,
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
        if (
            self.policy.require_plan_for_mutations
            and tool.permission in {PermissionLevel.WRITE, PermissionLevel.EXECUTE}
            and self.context.requires_replan
        ):
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error_kind=ErrorKind.PERMISSION_DENIED,
                output=f"update_plan is required after the previous failure before {call.name}",
            )
            return self._finish(task_id, call, result, started)
        assessment = self.policy.assess(task_id, call, tool, self.context)
        self._emit_security(task_id, call, assessment)
        if not assessment.allowed:
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error_kind=(
                    ErrorKind.PATH_DENIED
                    if assessment.reason.startswith("path escapes")
                    else ErrorKind.PERMISSION_DENIED
                ),
                output=assessment.reason,
            )
            return self._finish(task_id, call, result, started)
        try:
            arguments = tool.input_model.model_validate(call.arguments)
            output = tool.run(arguments, self.context)
            output_error = tool.classify_output(output)
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=output_error is None,
                output=output,
                error_kind=output_error,
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
        if not result.success and tool.permission in {
            PermissionLevel.WRITE,
            PermissionLevel.EXECUTE,
        }:
            self.context.requires_replan = True
        return self._finish(task_id, call, result, started)

    def _emit_security(
        self,
        task_id: str,
        call: ToolCall,
        assessment: RiskAssessment,
    ) -> None:
        if self.event_logger is not None:
            self.event_logger.emit(
                Event(
                    type="security.decision",
                    task_id=task_id,
                    data={
                        "call_id": call.id,
                        "tool_name": call.name,
                        "arguments": call.arguments,
                        "assessment": assessment.model_dump(mode="json"),
                    },
                )
            )

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
