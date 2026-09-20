"""Tool registration, validation, execution, timing, and error normalization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import suppress
from time import perf_counter

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from patchloop.domain import ErrorKind, ToolCall, ToolResult
from patchloop.events import Event, EventLogger
from patchloop.execution.policy import (
    ActionDescriptor,
    PolicyAction,
    PolicyEngine,
    PolicyEvaluation,
    PolicyRule,
    digest,
)
from patchloop.providers.base import ToolSpec
from patchloop.security import (
    RISK_ORDER,
    ApprovalRequest,
    PolicyDecision,
    RiskAssessment,
    RiskLevel,
    UnresolvedToolArgument,
    assert_executable_tool_arguments,
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
        *,
        policy_extension: bool = False,
        rules: tuple[PolicyRule, ...] = (),
        configuration_fingerprint: str = "",
    ) -> None:
        self.allowed_permissions = (
            frozenset({PermissionLevel.READ})
            if allowed_permissions is None
            else allowed_permissions
        )
        self.require_plan_for_mutations = require_plan_for_mutations
        self.approval_threshold = approval_threshold
        self.approval_handler = approval_handler
        self.policy_extension = policy_extension
        self.rules = rules
        self.configuration_fingerprint = configuration_fingerprint
        self.rule_loader: Callable[[str], list[PolicyRule]] | None = None
        self.engine = PolicyEngine()

    def allows(self, tool: Tool) -> bool:
        return tool.permission in self.allowed_permissions

    @property
    def version(self) -> str:
        payload = {
            "allowed_permissions": sorted(item.value for item in self.allowed_permissions),
            "approval_threshold": (
                None if self.approval_threshold is None else self.approval_threshold.value
            ),
            "require_plan_for_mutations": self.require_plan_for_mutations,
        }
        if self.policy_extension:
            payload["policy_extension"] = True
        if self.configuration_fingerprint:
            payload["configuration_fingerprint"] = self.configuration_fingerprint
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return f"tool-policy-v1:{digest}"

    def assess(
        self,
        task_id: str,
        call: ToolCall,
        tool: Tool,
        context: ToolContext,
    ) -> RiskAssessment:
        return self._assess(task_id, call, tool, context)

    def assess_for_preparation(
        self,
        task_id: str,
        call: ToolCall,
        tool: Tool,
        context: ToolContext,
    ) -> RiskAssessment:
        """Evaluate policy without consuming or requesting an approval."""

        return self._assess(task_id, call, tool, context)

    def evaluate(
        self, task_id: str, call: ToolCall, tool: Tool, context: ToolContext
    ) -> PolicyEvaluation:
        parsed = tool.input_model.model_validate(call.arguments)
        descriptor = tool.policy_descriptor(parsed, context)
        if (
            descriptor.tool_name != tool.name
            or descriptor.workspace_ref != str(context.repository)
            or descriptor.session_id != context.session_id
            or descriptor.arguments_fingerprint != digest(parsed.model_dump(mode="json"))
        ):
            raise ValueError("tool descriptor binding mismatch")
        rules = self.rules
        if self.rule_loader is not None:
            rules = (*rules, *self.rule_loader(descriptor.workspace_ref))
        evaluation = self.engine.evaluate(
            descriptor,
            rules=rules,
            policy_version=self.version,
            config_version=context.config_version,
        )
        legacy = self._legacy_assess(task_id, call, tool, context)
        reason: str | None = None
        if not self.allows(tool):
            reason = f"permission denied for {tool.permission} tool: {call.name}"
        elif legacy.decision is PolicyDecision.DENY:
            reason = legacy.reason
        elif self.require_plan_for_mutations and tool.permission != PermissionLevel.READ:
            if context.plan is None:
                reason = f"an execution plan is required before using {call.name}"
            elif context.requires_replan:
                reason = f"update_plan is required after the previous failure before {call.name}"
        if reason is not None:
            return evaluation.model_copy(update={"decision": PolicyDecision.DENY, "reason": reason})
        if (
            not self.policy_extension
            and not rules
            and descriptor.action in {PolicyAction.READ, PolicyAction.EDIT, PolicyAction.EXECUTE}
            and evaluation.decision is not PolicyDecision.DENY
        ):
            return evaluation.model_copy(
                update={"decision": legacy.decision, "reason": legacy.reason}
            )
        return evaluation

    def _assess(
        self, task_id: str, call: ToolCall, tool: Tool, context: ToolContext
    ) -> RiskAssessment:
        legacy = self._legacy_assess(task_id, call, tool, context)
        if legacy.decision is PolicyDecision.DENY:
            return legacy
        try:
            evaluation = self.evaluate(task_id, call, tool, context)
        except (ValueError, OSError):
            return RiskAssessment(
                risk=RiskLevel.CRITICAL,
                allowed=False,
                decision=PolicyDecision.DENY,
                reason="action descriptor cannot be safely normalized",
            )
        return RiskAssessment(
            risk=evaluation.risk,
            decision=evaluation.decision,
            allowed=evaluation.decision is PolicyDecision.ALLOW,
            approval_required=evaluation.decision is PolicyDecision.REQUIRE_APPROVAL,
            reason=evaluation.reason,
        )

    def _legacy_assess(
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
                decision=PolicyDecision.DENY,
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
                        decision=PolicyDecision.DENY,
                        reason="path escapes repository",
                    )
        approval_required = (
            self.approval_threshold is not None
            and RISK_ORDER[risk] >= RISK_ORDER[self.approval_threshold]
        )
        if not approval_required:
            return RiskAssessment(
                risk=risk,
                allowed=True,
                decision=PolicyDecision.ALLOW,
                reason=f"{tool.permission} action is within the authorized scope",
            )
        return RiskAssessment(
            risk=risk,
            allowed=False,
            approval_required=True,
            decision=PolicyDecision.REQUIRE_APPROVAL,
            reason=f"{risk} action requires operator approval",
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


class ToolPreparation(BaseModel):
    """Side-effect-free validation result captured with an Effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    action_kind: str
    normalized_arguments: dict[str, object] = Field(default_factory=dict)
    policy_result: RiskAssessment
    policy_evaluation: PolicyEvaluation | None = None
    action_descriptor: ActionDescriptor | None = None
    error: str | None = None


class ToolGateway:
    def __init__(
        self,
        context: ToolContext,
        tools: list[Tool],
        event_logger: EventLogger | None = None,
        policy: ToolPolicy | None = None,
        ownership_assertion: Callable[[PermissionLevel], None] | None = None,
    ) -> None:
        self.context = context
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("tool names must be unique")
        self.event_logger = event_logger
        self.policy = policy or ToolPolicy()
        self.ownership_assertion = ownership_assertion
        self.history: list[ToolResult] = []
        self.claim_validator: Callable[[str, ToolCall], bool] | None = None
        self._executed_claims: set[tuple[str, str]] = set()
        self._consumed_approval_calls: set[tuple[str, str]] = set()

    def specifications(self) -> list[ToolSpec]:
        return [tool.specification() for tool in self._tools.values()]

    def prepare_call(self, task_id: str, call: ToolCall) -> ToolPreparation:
        """Validate and normalize a call without invoking a tool or approval callback."""

        tool = self._tools.get(call.name)
        if tool is None:
            return ToolPreparation(
                tool_name=call.name,
                action_kind="unknown",
                normalized_arguments=call.arguments,
                policy_result=RiskAssessment(
                    risk=RiskLevel.CRITICAL,
                    allowed=False,
                    decision=PolicyDecision.DENY,
                    reason=f"unknown tool: {call.name}",
                ),
                error=f"unknown tool: {call.name}",
            )
        assessment = self.policy.assess_for_preparation(task_id, call, tool, self.context)
        evaluation = None
        with suppress(ValueError, OSError):
            evaluation = self.policy.evaluate(task_id, call, tool, self.context)
        error: str | None = None
        normalized: dict[str, object] = dict(call.arguments)
        if not self.policy.allows(tool):
            error = f"permission denied for {tool.permission} tool: {call.name}"
            assessment = assessment.model_copy(
                update={
                    "allowed": False,
                    "reason": error,
                    "approval_required": False,
                    "decision": PolicyDecision.DENY,
                }
            )
        elif call.arguments_error is not None:
            error = call.arguments_error
        else:
            try:
                assert_executable_tool_arguments(call.arguments)
                parsed = tool.input_model.model_validate(call.arguments)
                normalized = parsed.model_dump(mode="json")
            except (UnresolvedToolArgument, ValidationError) as exc:
                error = str(exc)
        if (
            error is None
            and self.policy.require_plan_for_mutations
            and tool.permission in {PermissionLevel.WRITE, PermissionLevel.EXECUTE}
            and self.context.plan is None
        ):
            error = f"an execution plan is required before using {call.name}"
        if (
            error is None
            and self.policy.require_plan_for_mutations
            and tool.permission in {PermissionLevel.WRITE, PermissionLevel.EXECUTE}
            and self.context.requires_replan
        ):
            error = f"update_plan is required after the previous failure before {call.name}"
        if error is not None:
            assessment = assessment.model_copy(
                update={
                    "allowed": False,
                    "approval_required": False,
                    "decision": PolicyDecision.DENY,
                    "reason": error,
                }
            )
        return ToolPreparation(
            tool_name=call.name,
            action_kind=tool.permission.value,
            normalized_arguments=normalized,
            policy_result=assessment,
            policy_evaluation=evaluation,
            action_descriptor=None if evaluation is None else evaluation.descriptor,
            error=error,
        )

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
        try:
            assert_executable_tool_arguments(call.arguments)
            tool.input_model.model_validate(call.arguments)
        except (UnresolvedToolArgument, ValidationError) as exc:
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error_kind=ErrorKind.INVALID_ARGUMENTS,
                output=str(exc),
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
        approval_consumed = (task_id, call.id) in self._consumed_approval_calls
        self._emit_security(
            task_id,
            call,
            assessment,
            approval_consumed=approval_consumed,
        )
        if assessment.decision is not PolicyDecision.ALLOW and not (
            approval_consumed and assessment.decision is PolicyDecision.REQUIRE_APPROVAL
        ):
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
        if self.ownership_assertion is not None:
            self.ownership_assertion(tool.permission)
        try:
            arguments = tool.input_model.model_validate(call.arguments)
            output = self.context._run_policy_checked(tool, arguments)
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

    def execute_claimed(
        self,
        task_id: str,
        call: ToolCall,
        *,
        approval_consumed: bool,
    ) -> ToolResult:
        """Execute a store-claimed Effect, honoring only its consumed exact approval."""

        key = (task_id, call.id)
        if (
            approval_consumed
            and key not in self._executed_claims
            and self.claim_validator is not None
            and self.claim_validator(task_id, call)
        ):
            self._consumed_approval_calls.add(key)
            self._executed_claims.add(key)
        try:
            return self.execute(task_id, call)
        finally:
            self._consumed_approval_calls.discard(key)

    def reject_prepared(
        self,
        task_id: str,
        call: ToolCall,
        reason: str,
        *,
        error_kind: ErrorKind | None = None,
    ) -> ToolResult:
        """Record a preparation-time rejection without invoking the tool backend."""

        started = perf_counter()
        preparation = self.prepare_call(task_id, call)
        kind = error_kind
        if kind is None:
            if call.name not in self._tools:
                kind = ErrorKind.UNKNOWN_TOOL
            elif preparation.policy_result.reason.startswith("path escapes"):
                kind = ErrorKind.PATH_DENIED
            elif (
                not self.policy.allows(self._tools[call.name])
                or preparation.policy_result.decision is PolicyDecision.DENY
            ):
                kind = ErrorKind.PERMISSION_DENIED
            elif call.arguments_error is not None:
                kind = ErrorKind.INVALID_ARGUMENTS
            else:
                kind = ErrorKind.EXECUTION_ERROR
        self._emit_security(task_id, call, preparation.policy_result)
        result = self._unexecuted_result(
            call,
            reason,
            effect_status="denied",
            next_action="choose_alternative",
            error_kind=kind,
        )
        if preparation.action_kind in {
            PermissionLevel.WRITE.value,
            PermissionLevel.EXECUTE.value,
        }:
            self.context.requires_replan = True
        return self._finish(task_id, call, result, started)

    def observe_unexecuted(
        self,
        task_id: str,
        call: ToolCall,
        reason: str,
        *,
        effect_status: str,
        next_action: str,
        error_kind: ErrorKind = ErrorKind.PERMISSION_DENIED,
    ) -> ToolResult:
        """Create the Provider observation for an action whose backend was not called."""

        started = perf_counter()
        result = self._unexecuted_result(
            call,
            reason,
            effect_status=effect_status,
            next_action=next_action,
            error_kind=error_kind,
        )
        tool = self._tools.get(call.name)
        if tool is not None and tool.permission in {
            PermissionLevel.WRITE,
            PermissionLevel.EXECUTE,
        }:
            self.context.requires_replan = True
        return self._finish(task_id, call, result, started)

    @staticmethod
    def _unexecuted_result(
        call: ToolCall,
        reason: str,
        *,
        effect_status: str,
        next_action: str,
        error_kind: ErrorKind,
    ) -> ToolResult:
        return ToolResult(
            call_id=call.id,
            tool_name=call.name,
            success=False,
            error_kind=error_kind,
            output=json.dumps(
                {
                    "backend_invoked": False,
                    "effect_status": effect_status,
                    "next_action": next_action,
                    "reason": reason,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )

    def _emit_security(
        self,
        task_id: str,
        call: ToolCall,
        assessment: RiskAssessment,
        *,
        approval_consumed: bool = False,
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
                        "approval_consumed": approval_consumed,
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
