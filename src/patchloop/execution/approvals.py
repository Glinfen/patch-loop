"""Persistent approval requests for prepared Effects."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from patchloop.domain import Task
from patchloop.execution.models import Approval, Effect

_APPROVAL_NAMESPACE = UUID("b27d9041-66dd-542c-85ff-5309bcc8bdf1")


class ApprovalPending(RuntimeError):
    """Control-flow signal indicating that Runtime must yield to the caller."""

    def __init__(self, approval: Approval) -> None:
        super().__init__(f"approval required: {approval.id}")
        self.approval = approval


class EffectPreauthorization(BaseModel):
    """An exact, non-transferable authorization candidate for one action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1)
    arguments_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    workspace_ref: str = Field(min_length=1)
    policy_version: str = Field(min_length=1, max_length=128)
    config_version: str = Field(min_length=1, max_length=128)

    def matches(
        self,
        effect: Effect,
        *,
        workspace_ref: str,
        policy_version: str,
        config_version: str,
    ) -> bool:
        return (
            self.tool_name == effect.tool_name
            and self.arguments_fingerprint == effect.arguments_fingerprint
            and self.workspace_ref == workspace_ref
            and self.policy_version == policy_version
            and self.config_version == config_version
        )


class ApprovalDecisionStore(Protocol):
    def resolve_effect_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        source: str,
        expected_version: int,
        workspace_ref: str,
        policy_version: str,
        config_version: str,
    ) -> tuple[Approval, Effect, Task]: ...


class ApprovalService:
    """Resolve approvals against the current execution conditions."""

    def __init__(self, store: ApprovalDecisionStore) -> None:
        self.store = store

    def decide(
        self,
        approval_id: str,
        *,
        approved: bool,
        source: str,
        expected_version: int,
        workspace_ref: str,
        policy_version: str,
        config_version: str,
    ) -> tuple[Approval, Effect, Task]:
        return self.store.resolve_effect_approval(
            approval_id,
            approved=approved,
            source=source,
            expected_version=expected_version,
            workspace_ref=workspace_ref,
            policy_version=policy_version,
            config_version=config_version,
        )


def stable_approval_id(effect: Effect, *, policy_version: str, config_version: str) -> str:
    identity = f"{effect.id}\0{effect.content_fingerprint()}\0{policy_version}\0{config_version}"
    return f"approval-{uuid5(_APPROVAL_NAMESPACE, identity).hex}"


def build_approval(
    task: Task,
    effect: Effect,
    *,
    policy_version: str,
    config_version: str,
) -> Approval:
    resources = [precondition.path for precondition in effect.file_preconditions]
    arguments_digest = hashlib.sha256(
        json.dumps(
            effect.arguments_summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return Approval(
        id=stable_approval_id(
            effect,
            policy_version=policy_version,
            config_version=config_version,
        ),
        effect_id=effect.id,
        effect_fingerprint=effect.content_fingerprint(),
        arguments_fingerprint=effect.arguments_fingerprint,
        workspace_ref=task.repository,
        action_summary=f"{effect.action_kind}:{effect.tool_name}:{arguments_digest}",
        resource_summary=(
            f"workspace={task.repository}; paths={','.join(resources)}"
            if resources
            else f"workspace={task.repository}"
        ),
        policy_version=policy_version,
        config_version=config_version,
    )


def match_exact_preauthorization(
    effect: Effect,
    candidates: Sequence[EffectPreauthorization],
    *,
    workspace_ref: str,
    policy_version: str,
    config_version: str,
) -> EffectPreauthorization | None:
    matches = [
        candidate
        for candidate in candidates
        if candidate.matches(
            effect,
            workspace_ref=workspace_ref,
            policy_version=policy_version,
            config_version=config_version,
        )
    ]
    if len(matches) > 1:
        raise ValueError("multiple preauthorizations match the same Effect")
    return matches[0] if matches else None


__all__ = [
    "ApprovalPending",
    "ApprovalService",
    "EffectPreauthorization",
    "build_approval",
    "match_exact_preauthorization",
    "stable_approval_id",
]
