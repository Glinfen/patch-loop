"""Persistent approval requests for prepared Effects."""

from __future__ import annotations

import builtins
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from patchloop.domain import Task
from patchloop.execution.models import Approval, ApprovalStatus, Effect, EffectStatus
from patchloop.execution.policy import (
    ActionDescriptor,
    ApprovalGrant,
    ApprovalScopeKind,
    PolicyEngine,
    PolicyEvaluation,
    PolicyRule,
)

if TYPE_CHECKING:
    from patchloop.persistence_contracts import LeaseGuard

_APPROVAL_NAMESPACE = UUID("b27d9041-66dd-542c-85ff-5309bcc8bdf1")


class ApprovalResolution(tuple[Approval, Effect, Task]):
    """One committed decision, including its grant; retains legacy three-value unpacking."""

    _grant: ApprovalGrant | None

    def __new__(
        cls, approval: Approval, effect: Effect, task: Task, grant: ApprovalGrant | None = None
    ) -> Self:
        result = super().__new__(cls, (approval, effect, task))
        result._grant = grant
        return result

    @property
    def approval(self) -> Approval:
        return self[0]

    @property
    def effect(self) -> Effect:
        return self[1]

    @property
    def task(self) -> Task:
        return self[2]

    @property
    def grant(self) -> ApprovalGrant | None:
        return self._grant


def validate_grant_consumption(
    grant: ApprovalGrant,
    descriptor: ActionDescriptor,
    *,
    rules: tuple[PolicyRule, ...],
    policy_version: str,
    config_version: str,
) -> None:
    from patchloop.persistence_contracts import ApprovalConflict

    evaluation = PolicyEngine().evaluate(
        descriptor,
        rules=rules,
        grants=(grant,),
        policy_version=policy_version,
        config_version=config_version,
    )
    if evaluation.matched_grant_id != grant.id:
        raise ApprovalConflict(grant.id, "grant_policy_mismatch", "consumed")


def supersede_approval(
    previous: Approval, original: Effect, replacement: Effect
) -> tuple[Approval, Effect]:
    if (
        original.task_id != replacement.task_id
        or original.id == replacement.id
        or original.status in {EffectStatus.EXECUTING, EffectStatus.UNKNOWN}
    ):
        raise ValueError("approval replacement requires a distinct unexecuted or settled effect")
    expired = previous
    if previous.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}:
        expired = previous.model_copy(
            update={"status": ApprovalStatus.EXPIRED, "version": previous.version + 1}
        )
    cancelled = original
    if original.status in {EffectStatus.PREPARED, EffectStatus.WAITING_FOR_APPROVAL}:
        cancelled = original.model_copy(
            update={"status": EffectStatus.CANCELLED, "version": original.version + 1}
        )
    return expired, cancelled


def validate_replacement(original: Effect, replacement: Effect, approval: Approval) -> None:
    if (
        original.status not in {EffectStatus.PREPARED, EffectStatus.WAITING_FOR_APPROVAL}
        or original.approval_id is None
        or replacement.id == original.id
        or replacement.supersedes_effect_id != original.id
        or replacement.task_id != original.task_id
        or replacement.provider_call_id != original.provider_call_id
        or replacement.identity_key() == original.identity_key()
        or replacement.status is not EffectStatus.PREPARED
        or replacement.approval_id is not None
        or replacement.approval_consumed
        or replacement.consumed_grant_id is not None
        or replacement.preparation_error is not None
        or replacement.action_descriptor is None
        or replacement.policy_result.get("decision") == "deny"
        or approval.supersedes_approval_id != original.approval_id
        or approval.status is not ApprovalStatus.PENDING
        or approval.effect_id != replacement.id
        or approval.effect_fingerprint != replacement.content_fingerprint()
    ):
        raise ValueError("invalid replacement Effect/Approval binding")


def evaluate_claim_policy(
    effect: Effect,
    task: Task,
    *,
    rules: tuple[PolicyRule, ...],
    grants: tuple[ApprovalGrant, ...],
    policy_version: str,
    config_version: str,
) -> PolicyEvaluation | None:
    from patchloop.persistence_contracts import ApprovalConflict
    from patchloop.security import PolicyDecision

    descriptor = effect.action_descriptor
    if descriptor is None:
        return None
    if (
        descriptor.workspace_ref != task.repository
        or descriptor.session_id != task.session_id
        or descriptor.arguments_fingerprint != effect.arguments_fingerprint
        or descriptor.tool_name != effect.tool_name
    ):
        raise ApprovalConflict(effect.id, "descriptor_binding_mismatch", "claimed")
    if effect.policy_evaluation is not None and (
        effect.policy_evaluation.policy_version != policy_version
        or effect.policy_evaluation.config_version != config_version
    ):
        raise ApprovalConflict(effect.id, "policy_version_mismatch", "claimed")
    evaluation = PolicyEngine().evaluate(
        descriptor,
        rules=rules,
        grants=grants if effect.retry_of_effect_id is None else (),
        policy_version=policy_version,
        config_version=config_version,
    )
    if evaluation.decision is PolicyDecision.DENY:
        raise ApprovalConflict(effect.id, "policy_denied", "claimed")
    if (
        effect.policy_evaluation is not None
        and effect.policy_evaluation.rules_fingerprint != evaluation.rules_fingerprint
    ):
        raise ApprovalConflict(effect.id, "rules_changed", "claimed")
    if (
        evaluation.decision is PolicyDecision.REQUIRE_APPROVAL
        and evaluation.matched_rule_ids
        and effect.policy_result.get("decision") == "allow"
    ):
        raise ApprovalConflict(effect.id, "policy_changed", "claimed")
    return evaluation


def scope_approval(
    approval: Approval,
    effect: Effect,
    task: Task,
    *,
    scope_kind: ApprovalScopeKind,
    expires_at: datetime | None,
    reason: str | None,
) -> tuple[Approval, ApprovalGrant | None]:
    """Derive an exact grant from a bound request, never from caller selectors."""
    from patchloop.security import SecretRedactor

    now = datetime.now(UTC)
    if expires_at is not None and (
        expires_at.tzinfo is None or expires_at <= now or expires_at > now + timedelta(days=1)
    ):
        raise ValueError("expiry must be timezone-aware, future, and within 24 hours")
    grant = None
    if scope_kind is not ApprovalScopeKind.ONCE:
        descriptor = effect.action_descriptor
        if descriptor is None:
            raise ValueError("scope authorization requires a normalized action descriptor")
        if descriptor.workspace_ref != task.repository or descriptor.session_id != task.session_id:
            raise ValueError("descriptor workspace or session binding mismatch")
        if descriptor.arguments_fingerprint != effect.arguments_fingerprint:
            raise ValueError("descriptor arguments binding mismatch")
        grant = ApprovalGrant(
            id=f"grant-{uuid5(_APPROVAL_NAMESPACE, approval.id).hex}",
            source_approval_id=approval.id,
            scope_kind=scope_kind,
            session_id=task.session_id if scope_kind is ApprovalScopeKind.SESSION else None,
            workspace_ref=descriptor.workspace_ref,
            action=descriptor.action,
            resources=descriptor.resources,
            arguments_fingerprint=descriptor.arguments_fingerprint,
            resource_state_fingerprint=descriptor.resource_state_fingerprint,
            rules_fingerprint=effect.policy_evaluation.rules_fingerprint
            if effect.policy_evaluation is not None
            else "",
            tool_name=descriptor.tool_name,
            policy_version=approval.policy_version,
            config_version=approval.config_version,
            expires_at=expires_at,
        )
    return approval.model_copy(
        update={
            "scope_kind": scope_kind,
            "expires_at": expires_at,
            "grant_id": None if grant is None else grant.id,
            "decision_reason": None if reason is None else SecretRedactor().redact_text(reason),
        }
    ), grant


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
    def replace_effect_approval(
        self,
        effect_id: str,
        replacement: Effect,
        approval: Approval,
        *,
        expected_version: int,
        lease_guard: LeaseGuard,
    ) -> tuple[Effect, Approval, Task]: ...

    def get_grant(self, grant_id: str) -> ApprovalGrant: ...

    def list_grants(self, scope_id: str) -> list[ApprovalGrant]: ...

    def revoke_grant(
        self, grant_id: str, *, source: str, reason: str | None = None, expected_version: int
    ) -> ApprovalGrant: ...

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
        scope_kind: ApprovalScopeKind = ApprovalScopeKind.ONCE,
        expires_at: datetime | None = None,
        reason: str | None = None,
    ) -> ApprovalResolution: ...


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

    def replace_request(
        self,
        approval_id: str,
        replacement: Effect,
        *,
        policy_version: str,
        config_version: str,
        lease_guard: LeaseGuard,
    ) -> tuple[Effect, Approval, Task]:
        previous = self.store.get_approval(approval_id)
        original = self.store.get_effect(previous.effect_id)
        task = self.store.get_task(original.task_id)
        request = build_approval(
            task,
            replacement,
            policy_version=policy_version,
            config_version=config_version,
            supersedes_approval_id=previous.id,
        )
        return self.store.replace_effect_approval(
            original.id,
            replacement,
            request,
            expected_version=original.version,
            lease_guard=lease_guard,
        )

    def list_grants(self, scope_id: str) -> builtins.list[ApprovalGrant]:
        return self.store.list_grants(scope_id)

    def revoke_grant(
        self, grant_id: str, *, source: str, reason: str | None = None
    ) -> ApprovalGrant:
        grant = self.store.get_grant(grant_id)
        return self.store.revoke_grant(
            grant_id, source=source, reason=reason, expected_version=grant.version
        )

    def decide_current(
        self,
        approval_id: str,
        *,
        approved: bool,
        source: str,
        scope_kind: ApprovalScopeKind = ApprovalScopeKind.ONCE,
        expires_at: datetime | None = None,
        reason: str | None = None,
    ) -> ApprovalResolution:
        """Decide using the request's persisted, exact execution binding."""

        approval = self.store.get_approval(approval_id)
        effect = self.store.get_effect(approval.effect_id)
        task = self.store.get_task(effect.task_id)
        return self.decide(
            approval.id,
            approved=approved,
            source=source,
            scope_kind=scope_kind,
            expires_at=expires_at,
            reason=reason,
            expected_version=approval.version,
            workspace_ref=task.repository,
            policy_version=approval.policy_version,
            config_version=approval.config_version,
        )

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
        scope_kind: ApprovalScopeKind = ApprovalScopeKind.ONCE,
        expires_at: datetime | None = None,
        reason: str | None = None,
    ) -> ApprovalResolution:
        return self.store.resolve_effect_approval(
            approval_id,
            approved=approved,
            source=source,
            scope_kind=scope_kind,
            expires_at=expires_at,
            reason=reason,
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
    supersedes_approval_id: str | None = None,
) -> Approval:
    resources = [precondition.path for precondition in effect.file_preconditions]
    if effect.action_descriptor is not None:
        resources = [
            f"{resource.kind.value}:{resource.value}"
            for resource in effect.action_descriptor.resources
        ]
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
            f"workspace={task.repository}; resources={','.join(resources)}"
            if resources
            else f"workspace={task.repository}"
        )[:2000],
        policy_version=policy_version,
        config_version=config_version,
        supersedes_approval_id=supersedes_approval_id,
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
    "ApprovalResolution",
    "ApprovalService",
    "EffectPreauthorization",
    "build_approval",
    "match_exact_preauthorization",
    "stable_approval_id",
]
