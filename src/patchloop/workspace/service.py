"""Workspace lifecycle, exact approvals and verified Git commits."""

from __future__ import annotations

import base64
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from patchloop.domain import TaskRuntimeCondition
from patchloop.events import SessionEvent
from patchloop.execution.approvals import ApprovalPending, build_approval
from patchloop.execution.models import Effect, EffectStatus
from patchloop.execution.ownership import ExecutionOwnership, ExecutionOwnershipManager
from patchloop.execution.policy import (
    ActionDescriptor,
    PolicyAction,
    PolicyEngine,
    ResourceKind,
    ResourceSelector,
    digest,
    normalize_command,
)
from patchloop.execution.policy_config import load_policy_configuration
from patchloop.sandbox import CommandSandbox, LocalProcessSandbox
from patchloop.security import PolicyDecision, RiskLevel, SecretRedactor
from patchloop.workspace.git import GitAdapter, canonical_path
from patchloop.workspace.models import (
    CommitPlan,
    OwnershipKind,
    VerificationInput,
    VerificationRecord,
    WorkspaceHandle,
    WorkspaceMode,
    WorkspaceStatus,
)
from patchloop.workspace.ownership import (
    ChangeOwnershipLedger,
    WorkspaceConflict,
    capture_baseline,
    file_state,
    safe_path,
)

if TYPE_CHECKING:
    from patchloop.persistence import SQLiteStore


class WorkspaceService:
    def __init__(
        self,
        store: SQLiteStore,
        *,
        git: GitAdapter | None = None,
        manager: ExecutionOwnershipManager | None = None,
        ownership: ExecutionOwnership | None = None,
        policy_version: str = "1",
        config_version: str = "1",
        sandbox: CommandSandbox | None = None,
    ) -> None:
        self.store = store
        self.git = git or GitAdapter()
        self.manager = manager or ExecutionOwnershipManager(store)
        self.ownership = ownership
        self.policy_version = policy_version
        self.config_version = config_version
        self.sandbox = sandbox or LocalProcessSandbox()

    def _assert(self, root: Path, session_id: str) -> ExecutionOwnership:
        ownership = self.ownership
        if ownership is None or ownership.workspace_lease is None:
            raise WorkspaceConflict("workspace writer lease required")
        self.manager.assert_owned(ownership)
        if (
            canonical_path(Path(ownership.workspace_lease.repository_path)) != canonical_path(root)
            or ownership.execution.session_id != session_id
        ):
            raise WorkspaceConflict("workspace lease binding mismatch")
        return ownership

    def _load(self, workspace_id: str) -> WorkspaceHandle:
        handle = self.store.get_workspace(workspace_id)
        if handle.status != WorkspaceStatus.OPEN:
            raise WorkspaceConflict(handle.recovery_advice or "workspace is not open")
        self._assert(handle.effective_root, handle.session_id)
        try:
            if handle.active_effect_id is not None or handle.cleanup_status == "creating":
                raise WorkspaceConflict("interrupted workspace operation requires reconciliation")
            self.store.get_baseline(handle.id)
            info = self.git.discover(handle.effective_root)
            if (
                info.identity != handle.repository.identity
                or info.revision.head != handle.base_revision
            ):
                raise WorkspaceConflict("workspace repository or HEAD changed")
            if (
                handle.mode == WorkspaceMode.DIRECT
                and info.revision.branch != handle.repository.revision.branch
            ):
                raise WorkspaceConflict("workspace branch changed")
            if handle.managed_worktree:
                marker = info.git_dir / "patchloop-owner"
                if (
                    info.git_dir != handle.worktree_git_dir
                    or not marker.is_file()
                    or marker.read_text(encoding="ascii") != handle.owner_nonce
                ):
                    raise WorkspaceConflict("managed worktree ownership marker mismatch")
            if (
                handle.mode == WorkspaceMode.WORKTREE
                and self.git.revision(handle.repository.repository_root)
                != handle.repository.revision
            ):
                raise WorkspaceConflict("source repository HEAD or branch changed")
        except Exception as exc:
            self._recovery(handle, str(exc))
            raise
        return handle

    def _recovery(
        self,
        handle: WorkspaceHandle,
        reason: str,
        *,
        cleanup_failed: bool = False,
    ) -> None:
        try:
            handle = self.store.get_workspace(handle.id)
        except KeyError:
            self.store.create_workspace(handle)
        handle.status = WorkspaceStatus.RECOVERY_REQUIRED
        if cleanup_failed:
            handle.cleanup_status = "failed"
        handle.recovery_advice = SecretRedactor().redact_text(reason)[:512]
        self.store.update_workspace(handle)
        self._event(handle, "workspace.recovery_required", {"reason": handle.recovery_advice})

    def _event(self, handle: WorkspaceHandle, event: str, data: dict[str, object]) -> None:
        self.store.append_event(
            SessionEvent(
                session_id=handle.session_id,
                task_id=None if self.ownership is None else self.ownership.execution.task_id,
                type=event,
                data={"workspace_id": handle.id, **data},
            )
        )

    def ledger(self, workspace_id: str) -> ChangeOwnershipLedger:
        handle = self._load(workspace_id)

        def assert_owned() -> None:
            self._assert(handle.effective_root, handle.session_id)

        return ChangeOwnershipLedger(
            handle,
            self.store,
            self.git,
            assert_owned,
        )

    def state_digest(self, handle: WorkspaceHandle) -> str:
        baseline = capture_baseline(handle, self.git)
        config = load_policy_configuration(
            handle.repository.repository_root, session_id=handle.session_id
        )
        workspace_ref = str(handle.effective_root)
        if self.ownership is not None:
            workspace_ref = self.store.get_task(self.ownership.execution.task_id).repository
        return digest(
            {
                "policy_configuration": config.fingerprint,
                "policy_rules": [
                    rule.model_dump(mode="json")
                    for rule in self.store.list_policy_rules(workspace_ref)
                ],
                "head": baseline.revision.model_dump(mode="json"),
                "files": baseline.digest,
                "index": baseline.index_digest,
                "status": [
                    item.model_dump(mode="json")
                    for item in baseline.status.paths
                    if item.path.split("/")[0] != ".patchloop"
                ],
            }
        )

    def _descriptor(
        self,
        handle: WorkspaceHandle,
        action: str,
        arguments: dict[str, object],
        state: str,
    ) -> ActionDescriptor:
        selected = arguments.get("paths", [])
        selectors = selected if isinstance(selected, list) else []
        return ActionDescriptor(
            action=PolicyAction.GIT,
            tool_name="workspace." + action,
            workspace_ref=str(handle.effective_root),
            session_id=handle.session_id,
            resources=(
                ResourceSelector(kind=ResourceKind.WORKSPACE, value=handle.id),
                *(
                    ResourceSelector(kind=ResourceKind.PATH, value=path)
                    for path in selectors
                    if isinstance(path, str)
                ),
            ),
            arguments_fingerprint=digest(arguments),
            resource_state_fingerprint=digest(
                {
                    "root": str(handle.effective_root),
                    "base": handle.base_revision,
                    "state": state,
                }
            ),
            side_effect=True,
            risk=RiskLevel.HIGH,
        )

    def _authorized[T](
        self,
        handle: WorkspaceHandle,
        action: str,
        arguments: dict[str, object],
        operation: Callable[[], T],
    ) -> T:
        ownership = self._assert(handle.effective_root, handle.session_id)
        state = self.state_digest(handle)
        task = self.store.get_task(ownership.execution.task_id)
        descriptor = self._descriptor(handle, action, arguments, state).model_copy(
            update={"workspace_ref": task.repository}
        )
        if Path(task.repository).resolve() != handle.effective_root.resolve():
            raise WorkspaceConflict("task repository does not match workspace action")
        configuration = load_policy_configuration(
            handle.repository.repository_root, session_id=handle.session_id
        )
        configured_rules = tuple(
            rule.model_copy(update={"workspace_ref": task.repository})
            for rule in configuration.rules_for(self.policy_version)
        )
        evaluation = PolicyEngine().evaluate(
            descriptor,
            rules=(*configured_rules, *self.store.list_policy_rules(task.repository)),
            grants=tuple(self.store.list_grants(handle.session_id)),
            policy_version=self.policy_version,
            config_version=self.config_version,
        )
        if evaluation.decision == PolicyDecision.DENY:
            self._event(handle, "workspace.policy_denied", {"descriptor": descriptor.digest})
            raise PermissionError(evaluation.reason)
        effect_id = digest(
            {
                "task": task.id,
                "descriptor": descriptor.digest,
                "policy": self.policy_version,
                "config": self.config_version,
            }
        )
        try:
            effect = self.store.get_effect(effect_id)
        except KeyError:
            effect = Effect(
                id=effect_id,
                task_id=task.id,
                step_id="workspace:" + effect_id,
                batch_position=0,
                provider_call_id=effect_id,
                tool_name=descriptor.tool_name,
                action_kind="git",
                arguments_summary=arguments,
                arguments_fingerprint=descriptor.arguments_fingerprint,
                action_descriptor=descriptor,
                policy_evaluation=evaluation,
            )
            effect = self.store.prepare_effects(
                [effect], expected_version=task.version, lease_guard=ownership.lease_guard
            )[0]
        if effect.status in {EffectStatus.EXECUTING, EffectStatus.UNKNOWN}:
            raise WorkspaceConflict("interrupted workspace operation requires recovery")
        if effect.status == EffectStatus.SUCCEEDED:
            raise WorkspaceConflict("operation already completed; inspect workspace status")
        explicit = action in {"commit", "close", "open_worktree"}
        approved = (
            effect.approval_id is not None
            and self.store.get_approval(effect.approval_id).status.value == "approved"
        )
        if not approved and (explicit or evaluation.decision == PolicyDecision.REQUIRE_APPROVAL):
            request = build_approval(
                task, effect, policy_version=self.policy_version, config_version=self.config_version
            )
            _, approval, _ = self.store.request_effect_approval(
                effect.id,
                request,
                expected_version=effect.version,
                lease_guard=ownership.lease_guard,
            )
            raise ApprovalPending(approval)
        if self.state_digest(handle) != state:
            raise WorkspaceConflict("workspace changed after authorization")
        claimed = self.store.claim_effect(
            effect.id,
            expected_version=effect.version,
            lease_guard=ownership.lease_guard,
            effect_fingerprint=effect.content_fingerprint(),
            workspace_ref=task.repository,
            policy_version=self.policy_version,
            config_version=self.config_version,
            current_evaluation=evaluation,
            configured_rules=configured_rules,
        )
        current_task = self.store.get_task(task.id)
        if current_task.runtime_condition != TaskRuntimeCondition.RUNNING:
            self.store.update_task(
                current_task.model_copy(update={"runtime_condition": TaskRuntimeCondition.RUNNING}),
                expected_version=current_task.version,
                lease_guard=ownership.lease_guard,
            )
        self._assert(handle.effective_root, handle.session_id)
        try:
            tracked = self.store.get_workspace(handle.id)
        except KeyError:
            tracked = self.store.create_workspace(handle)
        tracked.active_effect_id = effect.id
        self.store.update_workspace(tracked)
        try:
            result = operation()
        except Exception:
            failed = claimed.model_copy(update={"status": EffectStatus.UNKNOWN})
            self.store.commit_effect(
                failed,
                expected_version=claimed.version,
                result_ref=None,
                observation_ref=None,
                lease_guard=ownership.lease_guard,
            )
            self._recovery(
                handle,
                "workspace operation failed; inspect effect " + effect.id,
                cleanup_failed=action == "close",
            )
            raise
        succeeded = claimed.model_copy(update={"status": EffectStatus.SUCCEEDED})
        self.store.commit_effect(
            succeeded,
            expected_version=claimed.version,
            result_ref=handle.id,
            observation_ref=None,
            lease_guard=ownership.lease_guard,
        )
        tracked = self.store.get_workspace(handle.id)
        tracked.active_effect_id = None
        self.store.update_workspace(tracked)
        self._event(handle, "workspace." + action, {"effect_id": effect.id, "state_digest": state})
        return result

    def open(
        self,
        session_id: str,
        repository: Path,
        *,
        mode: WorkspaceMode = WorkspaceMode.DIRECT,
        base_revision: str | None = None,
        legacy_direct: bool = False,
    ) -> WorkspaceHandle:
        info = self.git.discover(repository)
        session = self.store.get_session(session_id)
        if canonical_path(Path(session.workspace_ref)) != info.repository_root:
            raise WorkspaceConflict("session repository mismatch")
        existing = [
            item
            for item in self.store.list_workspaces(session_id)
            if item.status != WorkspaceStatus.CLOSED
        ]
        if existing:
            if existing[0].mode != mode:
                raise WorkspaceConflict("session already has another workspace mode")
            if existing[0].cleanup_status != "creating":
                return self._load(existing[0].id)
            if existing[0].active_effect_id is not None:
                self._recovery(
                    existing[0], "worktree creation was interrupted; inspect managed path"
                )
                raise WorkspaceConflict("worktree creation requires reconciliation")
            if info.revision != existing[0].repository.revision:
                self._recovery(existing[0], "source revision changed during worktree creation")
                raise WorkspaceConflict("source revision changed")
            if base_revision is None:
                base_revision = existing[0].base_revision
        self._assert(info.repository_root, session_id)
        revision = self.git.resolve_revision(info.repository_root, base_revision or "HEAD")
        if mode == WorkspaceMode.DIRECT and revision != info.revision.head:
            raise WorkspaceConflict("direct mode must use current HEAD")
        handle = WorkspaceHandle(
            id=digest(
                {
                    "session": session_id,
                    "repository": str(info.repository_root),
                    "mode": mode,
                    "revision": revision,
                    "generation": len(self.store.list_workspaces(session_id)),
                }
            ),
            session_id=session_id,
            repository=info,
            effective_root=info.repository_root,
            mode=mode,
            base_revision=revision,
            legacy_direct=legacy_direct,
        )
        if existing:
            handle = existing[0]
        ownership = self._assert(info.repository_root, session_id)
        handle.lease_owner = ownership.execution.owner_id
        handle.lease_generation = (
            ownership.workspace_lease.generation if ownership.workspace_lease else None
        )
        if mode == WorkspaceMode.WORKTREE:
            dirty = [
                item
                for item in self.git.status(info.repository_root).paths
                if item.path.split("/")[0] != ".patchloop"
            ]
            if dirty:
                raise WorkspaceConflict("worktree mode requires a clean source repository")
            target = info.repository_root / ".patchloop" / "worktrees" / handle.id
            for parent in (target.parent, target.parent.parent):
                if parent.is_symlink() or parent.is_junction():
                    raise WorkspaceConflict("managed worktree parent is an alias")
            if not target.resolve().is_relative_to(info.repository_root.resolve()):
                raise WorkspaceConflict("managed worktree destination escapes source repository")
            if target.exists() or target.is_symlink():
                raise WorkspaceConflict("managed worktree destination already exists")
            handle.cleanup_status = "creating"
            if not existing:
                self.store.create_workspace(handle)
            self._authorized(
                handle,
                "open_worktree",
                {"revision": revision, "target": str(target)},
                lambda: self.git.create_worktree(info.repository_root, target, revision),
            )
            handle.effective_root = target.resolve()
            handle.managed_worktree = True
            handle.worktree_git_dir = self.git.discover(target).git_dir
            (handle.worktree_git_dir / "patchloop-owner").write_text(
                handle.owner_nonce, encoding="ascii"
            )
            handle.cleanup_status = "pending"
        baseline = capture_baseline(handle, self.git)
        handle.baseline_digest = baseline.digest
        if mode == WorkspaceMode.WORKTREE:
            current = self.store.get_workspace(handle.id)
            handle.version = current.version
            handle.active_effect_id = None
            handle = self.store.update_workspace(handle)
        else:
            self.store.create_workspace(handle)
        self.store.save_baseline(baseline)
        self._event(handle, "workspace.open", {"mode": mode.value})
        return handle

    def status(self, workspace_id: str) -> dict[str, object]:
        handle = self.store.get_workspace(workspace_id)
        changes = self.ledger(workspace_id).refresh()
        git_state = self.git.status(handle.effective_root)
        paths = [item for item in git_state.paths if item.path.split("/")[0] != ".patchloop"]
        return {
            "workspace": handle.model_dump(mode="json"),
            "head": self.git.revision(handle.effective_root).model_dump(mode="json"),
            "dirty_summary": {
                "staged": sum(item.index != "." for item in paths),
                "unstaged": sum(item.worktree != "." for item in paths),
                "untracked": sum(item.untracked for item in paths),
            },
            "changes": [item.model_dump(mode="json", exclude={"baseline"}) for item in changes],
            "verifications": [
                item.model_dump(mode="json") for item in self.store.list_verifications(workspace_id)
            ],
        }

    def diff(self, workspace_id: str, scope: str = "default") -> dict[str, object]:
        return self.ledger(workspace_id).diff(scope)

    def accept(self, workspace_id: str, paths: Sequence[str]) -> None:
        self.ledger(workspace_id).accept(paths)
        self._event(
            self.store.get_workspace(workspace_id), "workspace.accept", {"paths": list(paths)}
        )

    def revert(self, workspace_id: str, paths: Sequence[str]) -> dict[str, object]:
        ledger = self.ledger(workspace_id)
        return self._authorized(
            ledger.handle, "revert", {"paths": sorted(paths)}, lambda: ledger.revert(paths)
        )

    def verify(self, workspace_id: str, result: VerificationInput) -> VerificationRecord:
        handle = self._load(workspace_id)
        normalize_command(result.command)
        state = self.state_digest(handle)

        def execute() -> VerificationRecord:
            output = self.sandbox.execute(
                result.command, handle.effective_root, timeout_seconds=300, max_output_chars=4000
            )
            self._assert(handle.effective_root, handle.session_id)
            if state != self.state_digest(handle):
                raise WorkspaceConflict("verification changed workspace; rerun on a stable version")
            record = VerificationRecord(
                workspace_id=handle.id,
                effective_root=handle.effective_root,
                head=handle.base_revision or "",
                digest=state,
                command=result.command,
                returncode=output.exit_code,
                result_summary=SecretRedactor().redact_text(output.output),
                policy_version=self.policy_version,
                config_version=self.config_version,
            )
            self.store.save_verification(record)
            return record

        return self._authorized(handle, "verify", {"command": result.command}, execute)

    def prepare_commit(self, workspace_id: str, message: str) -> CommitPlan:
        handle = self._load(workspace_id)
        if not message.strip() or SecretRedactor().redact_text(message) != message:
            raise ValueError("commit message is empty or contains credentials")
        changes = self.ledger(workspace_id).refresh()
        if any(item.ownership == OwnershipKind.MIXED for item in changes):
            raise WorkspaceConflict("mixed changes prevent commit")
        paths = [
            item.path
            for item in changes
            if item.ownership == OwnershipKind.AGENT
            and item.accepted
            and item.accepted_digest == item.current_digest
        ]
        if not paths:
            raise WorkspaceConflict("no accepted agent changes")
        state = self.state_digest(handle)
        verifications = self.store.list_verifications(workspace_id)
        verification = verifications[0] if verifications else None
        if (
            verification is None
            or verification.digest != state
            or verification.returncode != 0
            or verification.head != handle.base_revision
            or verification.effective_root != handle.effective_root
            or verification.policy_version != self.policy_version
            or verification.config_version != self.config_version
        ):
            raise WorkspaceConflict("successful verification of current version required")
        plan = CommitPlan(
            workspace_id=workspace_id,
            message=message,
            head=handle.base_revision or "",
            digest=state,
            paths=paths,
            verification_id=verification.id,
            policy_version=self.policy_version,
            config_version=self.config_version,
        )
        self.store.save_commit_plan(plan)
        return plan

    def commit(self, workspace_id: str, plan_id: str) -> dict[str, object]:
        plan = self.store.get_commit_plan(plan_id)
        if plan.workspace_id != workspace_id:
            raise WorkspaceConflict("commit plan belongs to another workspace")
        if plan.commit_revision is not None:
            return {
                "commit": plan.commit_revision,
                "workspace_id": workspace_id,
                "plan_id": plan_id,
            }
        handle = self._load(workspace_id)
        fresh = self.prepare_commit(workspace_id, plan.message)
        if (
            fresh.digest,
            fresh.paths,
            fresh.head,
            fresh.verification_id,
            fresh.policy_version,
            fresh.config_version,
        ) != (
            plan.digest,
            plan.paths,
            plan.head,
            plan.verification_id,
            plan.policy_version,
            plan.config_version,
        ):
            raise WorkspaceConflict("commit plan is stale")

        def execute() -> dict[str, object]:
            contents: dict[str, tuple[bytes, int] | None] = {}
            for relative in plan.paths:
                state = file_state(safe_path(handle.effective_root, relative))
                if state.kind not in {"text", "binary", "missing"}:
                    raise WorkspaceConflict("unsupported commit path")
                contents[relative] = (
                    None
                    if state.kind == "missing"
                    else (
                        base64.b64decode(state.content_base64 or ""),
                        0o100755 if (state.mode or 0) & 0o111 else 0o100644,
                    )
                )
            revision = self.git.build_commit(
                handle.effective_root, plan.head, plan.message, contents
            )
            self._assert(handle.effective_root, handle.session_id)
            if self.state_digest(handle) != plan.digest:
                raise WorkspaceConflict("workspace changed while constructing commit")
            self.git.advance_head(handle.effective_root, revision, plan.head)
            if handle.mode == WorkspaceMode.WORKTREE:
                self.git.synchronize_owned_index(handle.effective_root, revision, plan.paths)
            plan.commit_revision = revision
            self.store.save_commit_plan(plan)
            for change in self.store.list_changes(handle.id):
                if change.path in plan.paths:
                    change.accepted = False
                    change.accepted_digest = None
                    self.store.save_change(change)
            handle.base_revision = revision
            handle.version = self.store.get_workspace(handle.id).version
            self.store.update_workspace(handle)
            return {"workspace_id": workspace_id, "commit": revision, "plan_id": plan_id}

        return self._authorized(handle, "commit", plan.model_dump(mode="json"), execute)

    def close(self, workspace_id: str) -> None:
        handle = self.store.get_workspace(workspace_id)
        if handle.status == WorkspaceStatus.CLOSED:
            return
        self._load(workspace_id)

        def execute() -> None:
            if handle.managed_worktree:
                expected = (
                    handle.repository.repository_root / ".patchloop" / "worktrees" / handle.id
                )
                if any(
                    path.is_symlink() or path.is_junction()
                    for path in (expected, expected.parent, expected.parent.parent)
                ):
                    raise WorkspaceConflict("managed worktree path was replaced with an alias")
                if (
                    canonical_path(expected) != canonical_path(handle.effective_root)
                    or self.git.discover(expected).identity != handle.repository.identity
                ):
                    raise WorkspaceConflict("managed worktree identity mismatch")
                self.git.remove_worktree(handle.repository.repository_root, expected)
                handle.cleanup_status = "complete"
            handle.status = WorkspaceStatus.CLOSED
            handle.version = self.store.get_workspace(handle.id).version
            self.store.update_workspace(handle)

        self._authorized(handle, "close", {"workspace_id": workspace_id}, execute)
