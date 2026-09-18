"""Persistent approval requests for prepared Effects."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from patchloop.domain import Task
from patchloop.execution.models import Approval, Effect
from patchloop.execution.policy import ApprovalGrant, ApprovalScopeKind, PolicyAction

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
    def get_approval(self, approval_id: str) -> Approval: ...

    def list_approvals(self, task_id: str) -> list[Approval]: ...

    def get_effect(self, effect_id: str) -> Effect: ...

    def get_task(self, task_id: str) -> Task: ...

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

    def save_approval_grant(self, grant: ApprovalGrant) -> ApprovalGrant: ...

    def list_approval_grants(self, scope_id: str) -> list[ApprovalGrant]: ...

    def revoke_approval_grant(
        self, grant_id: str, *, expected_version: int | None = None
    ) -> ApprovalGrant: ...


class ApprovalService:
    """Resolve approvals against the current execution conditions."""

    def __init__(self, store: ApprovalDecisionStore) -> None:
        self.store = store

    def get(self, approval_id: str) -> Approval:
        return self.store.get_approval(approval_id)

    def list(self, task_id: str) -> list[Approval]:
        return self.store.list_approvals(task_id)

    def task(self, task_id: str) -> Task:
        return self.store.get_task(task_id)

    def decide_current(
        self,
        approval_id: str,
        *,
        approved: bool,
        source: str,
        scope_kind: ApprovalScopeKind = ApprovalScopeKind.ONCE,
        expires_at: datetime | None = None,
        reason: str | None = None,
    ) -> tuple[Approval, Effect, Task]:
        """Decide using the request's persisted, exact execution binding."""

        approval = self.store.get_approval(approval_id)
        effect = self.store.get_effect(approval.effect_id)
        task = self.store.get_task(effect.task_id)
        resolved = self.decide(
            approval.id,
            approved=approved,
            source=source,
            expected_version=approval.version,
            workspace_ref=task.repository,
            policy_version=approval.policy_version,
            config_version=approval.config_version,
        )
        decided, effect, task = resolved
        if reason is not None and reason != decided.decision_reason:
            decided = decided.model_copy(update={"decision_reason": reason})
        if scope_kind is not ApprovalScopeKind.ONCE and approved:
            action = {
                "read": PolicyAction.READ,
                "write": PolicyAction.EDIT,
                "execute": PolicyAction.EXECUTE,
            }.get(effect.action_kind, PolicyAction.EXECUTE)
            grant = ApprovalGrant(
                id=f"grant-{decided.id}",
                source_approval_id=decided.id,
                scope_kind=scope_kind,
                session_id=task.session_id if scope_kind is ApprovalScopeKind.SESSION else None,
                workspace_ref=task.repository,
                action=action,
                policy_version=decided.policy_version,
                config_version=decided.config_version,
                expires_at=expires_at,
            )
            self.store.save_approval_grant(grant)
            decided = decided.model_copy(
                update={
                    "scope_kind": scope_kind,
                    "grant_id": grant.id,
                    "expires_at": expires_at,
                    "decision_reason": reason,
                }
            )
        return decided, effect, task

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

    def list_grants(self, scope_id: str) -> list[ApprovalGrant]:
        return self.store.list_approval_grants(scope_id)

    def revoke_grant(self, grant_id: str, *, expected_version: int | None = None) -> ApprovalGrant:
        return self.store.revoke_approval_grant(grant_id, expected_version=expected_version)


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


def build_superseding_approval(
    task: Task,
    effect: Effect,
    previous: Approval,
    *,
    policy_version: str,
    config_version: str,
) -> Approval:
    """Create a fresh request while retaining an auditable supersedes link."""
    return build_approval(
        task,
        effect,
        policy_version=policy_version,
        config_version=config_version,
    ).model_copy(update={"supersedes_approval_id": previous.id})


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
    "build_superseding_approval",
    "match_exact_preauthorization",
    "stable_approval_id",
]
