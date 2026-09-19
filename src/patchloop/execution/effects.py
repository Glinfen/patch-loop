"""Stable Effect identities and persisted provider-response batches."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from patchloop.domain import AgentStep, Task, ToolCall, ToolResult
from patchloop.execution.models import Effect, EffectStatus, FileEffectPrecondition
from patchloop.providers.base import ModelResponse
from patchloop.security import PolicyDecision
from patchloop.tools.gateway import ToolGateway
from patchloop.tools.write import preview_file_mutation, supports_file_mutation_preview

_EFFECT_NAMESPACE = UUID("d672ff31-1843-5dd3-b35d-3c82920aa12c")


class ReconcileOutcome(StrEnum):
    UNCHANGED = "unchanged"
    CONFIRMED_RESULT = "confirmed_result"
    RECOVERY_REQUIRED = "recovery_required"


class EffectReconciliation(BaseModel):
    """Side-effect-free recovery assessment for one persisted Effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: ReconcileOutcome
    evidence: dict[str, object] = Field(default_factory=dict)
    result: ToolResult | None = None


def stable_effect_id(task_id: str, step_id: str, batch_position: int) -> str:
    """Return the execution identity for one fixed position in a model batch."""

    identity = f"{task_id}\0{step_id}\0{batch_position}"
    return f"effect-{uuid5(_EFFECT_NAMESPACE, identity).hex}"


def persist_model_response_batch(
    task: Task,
    step: AgentStep,
    response: ModelResponse,
    gateway: ToolGateway,
) -> tuple[AgentStep, list[Effect]]:
    """Attach a complete response and its ordered, stable Effects to a Step."""

    effects: list[Effect] = []
    projected_files: dict[str, str | None] = {}
    for position, call in enumerate(response.tool_calls):
        preparation = gateway.prepare_call(task.id, call)
        preparation_error = preparation.error
        preconditions: list[FileEffectPrecondition] = []
        if (
            preparation.action_kind == "write"
            and preparation_error is None
            and supports_file_mutation_preview(call.name)
        ):
            try:
                raw_path = preparation.normalized_arguments.get("path")
                if not isinstance(raw_path, str):
                    raise ValueError(f"file mutation requires a path: {call.name}")
                candidate = gateway.context.resolve_path(raw_path, must_exist=False)
                relative_path = candidate.relative_to(gateway.context.repository).as_posix()
                if relative_path in projected_files:
                    preview = preview_file_mutation(
                        call.name,
                        preparation.normalized_arguments,
                        gateway.context,
                        current_content=projected_files[relative_path],
                    )
                else:
                    preview = preview_file_mutation(
                        call.name,
                        preparation.normalized_arguments,
                        gateway.context,
                    )
                projected_files[relative_path] = preview.target_content
                gateway.context.changes.capture_original(preview.path, preview.original_content)
                preconditions.append(
                    FileEffectPrecondition(
                        path=preview.relative_path,
                        existed=preview.original_content is not None,
                        original_content=preview.original_content,
                        original_sha256=(
                            None
                            if preview.original_content is None
                            else _content_fingerprint(preview.original_content)
                        ),
                        target_sha256=_content_fingerprint(preview.target_content),
                    )
                )
            except (OSError, ValueError) as exc:
                preparation_error = str(exc)
                preparation = preparation.model_copy(
                    update={
                        "error": preparation_error,
                        "policy_result": preparation.policy_result.model_copy(
                            update={
                                "allowed": False,
                                "approval_required": False,
                                "decision": PolicyDecision.DENY,
                                "reason": preparation_error,
                            }
                        ),
                    }
                )
        effects.append(
            Effect(
                id=stable_effect_id(task.id, step.id, position),
                task_id=task.id,
                step_id=step.id,
                batch_position=position,
                provider_call_id=call.id,
                tool_name=call.name,
                action_kind=preparation.action_kind,
                arguments_summary=preparation.normalized_arguments,
                arguments_fingerprint=arguments_fingerprint(preparation.normalized_arguments),
                policy_result=preparation.policy_result.model_dump(mode="json"),
                preparation_error=preparation_error,
                file_preconditions=preconditions,
            )
        )
    persisted_step = step.model_copy(
        update={
            "decision": response.content,
            "model_response": response.model_dump(mode="json"),
            "effect_ids": [effect.id for effect in effects],
        }
    )
    return persisted_step, effects


def response_from_step(step: AgentStep) -> ModelResponse | None:
    """Load the original provider response without asking the provider again."""

    if step.model_response is None:
        return None
    return ModelResponse.model_validate(step.model_response)


def restore_file_preconditions(gateway: ToolGateway, effects: list[Effect]) -> None:
    """Rehydrate diff baselines from protected Effect data after a restart."""

    for effect in effects:
        for precondition in effect.file_preconditions:
            path = gateway.context.resolve_path(precondition.path, must_exist=False)
            gateway.context.changes.capture_original(path, precondition.original_content)


def revalidate_effect_call(effect: Effect, call: ToolCall, gateway: ToolGateway) -> ToolCall:
    """Revalidate persisted input and policy immediately before claiming an Effect."""

    if call.id != effect.provider_call_id or call.name != effect.tool_name:
        raise ValueError(f"provider call no longer matches prepared Effect: {effect.id}")
    if effect.preparation_error is not None:
        raise ValueError(effect.preparation_error)
    preparation = gateway.prepare_call(effect.task_id, call)
    if preparation.error is not None:
        raise ValueError(preparation.error)
    if preparation.action_kind != effect.action_kind:
        raise ValueError(f"action kind changed for prepared Effect: {effect.id}")
    if arguments_fingerprint(preparation.normalized_arguments) != effect.arguments_fingerprint:
        raise ValueError(f"arguments changed for prepared Effect: {effect.id}")
    persisted_decision = effect.policy_result.get("decision")
    current_decision = preparation.policy_result.decision or PolicyDecision.DENY
    if current_decision is PolicyDecision.DENY:
        raise ValueError(preparation.policy_result.reason)
    if current_decision.value != persisted_decision:
        raise ValueError(f"policy decision changed for prepared Effect: {effect.id}")
    return call.model_copy(update={"arguments": preparation.normalized_arguments})


def assert_file_preconditions(effect: Effect, gateway: ToolGateway) -> None:
    """Require every protected file to still match its prepared baseline."""

    for precondition in effect.file_preconditions:
        path = gateway.context.resolve_path(precondition.path, must_exist=False)
        if path.exists() != precondition.existed:
            raise ValueError(f"file precondition changed: {precondition.path}")
        if not precondition.existed:
            continue
        try:
            current = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot verify file precondition: {precondition.path}") from exc
        if _content_fingerprint(current) != precondition.original_sha256:
            raise ValueError(f"file precondition changed: {precondition.path}")


def reconcile_effect(
    effect: Effect,
    gateway: ToolGateway,
    *,
    persisted_result: ToolResult | None = None,
) -> EffectReconciliation:
    """Inspect an Effect after restart without invoking its backend."""

    if effect.status is not EffectStatus.EXECUTING:
        return EffectReconciliation(
            outcome=ReconcileOutcome.UNCHANGED,
            evidence={"persisted_status": effect.status.value},
        )
    if persisted_result is not None:
        return EffectReconciliation(
            outcome=ReconcileOutcome.CONFIRMED_RESULT,
            evidence={"source": "persisted_tool_result"},
            result=persisted_result,
        )
    if effect.file_preconditions:
        observations: list[dict[str, object]] = []
        targets_match = True
        for precondition in effect.file_preconditions:
            path = gateway.context.resolve_path(precondition.path, must_exist=False)
            exists = path.is_file()
            observed_sha256 = None
            if exists:
                try:
                    observed_sha256 = _content_fingerprint(path.read_text(encoding="utf-8"))
                except OSError:
                    targets_match = False
            if observed_sha256 != precondition.target_sha256:
                targets_match = False
            observations.append(
                {
                    "path": precondition.path,
                    "exists": exists,
                    "observed_sha256": observed_sha256,
                    "target_sha256": precondition.target_sha256,
                }
            )
        if targets_match:
            return EffectReconciliation(
                outcome=ReconcileOutcome.CONFIRMED_RESULT,
                evidence={"source": "file_target_digest", "files": observations},
                result=ToolResult(
                    call_id=effect.provider_call_id,
                    tool_name=effect.tool_name,
                    success=True,
                    output="Recovered completed file action from its persisted target digest.",
                ),
            )
        return EffectReconciliation(
            outcome=ReconcileOutcome.RECOVERY_REQUIRED,
            evidence={"source": "file_state_unconfirmed", "files": observations},
        )
    return EffectReconciliation(
        outcome=ReconcileOutcome.RECOVERY_REQUIRED,
        evidence={
            "source": "result_missing",
            "action_kind": effect.action_kind,
            "tool_name": effect.tool_name,
        },
    )


def arguments_fingerprint(arguments: dict[str, object]) -> str:
    payload = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _content_fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


__all__ = [
    "EffectReconciliation",
    "ReconcileOutcome",
    "arguments_fingerprint",
    "assert_file_preconditions",
    "persist_model_response_batch",
    "reconcile_effect",
    "response_from_step",
    "restore_file_preconditions",
    "revalidate_effect_call",
    "stable_effect_id",
]
