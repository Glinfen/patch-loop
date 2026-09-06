"""Ordered Session, Task, and Workspace execution ownership acquisition."""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from patchloop.events import (
    SessionEvent,
    journal_event_id,
    lease_owner_summary,
)
from patchloop.execution.models import Execution, WorkspaceLease
from patchloop.persistence_contracts import (
    LeaseConflict,
    LeaseGuard,
    LeaseLost,
    RecoveryRequired,
    WorkspaceLeaseGuard,
)
from patchloop.sandbox import (
    ManagedCommandIdentity,
    ManagedCommandStatus,
    reconcile_managed_command,
)

DEFAULT_LEASE_TTL = timedelta(seconds=60)
DEFAULT_HEARTBEAT_INTERVAL = timedelta(seconds=20)

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]
TokenFactory = Callable[[], str]


@dataclass(frozen=True)
class LeasePolicy:
    ttl: timedelta = DEFAULT_LEASE_TTL
    heartbeat_interval: timedelta = DEFAULT_HEARTBEAT_INTERVAL

    def __post_init__(self) -> None:
        if self.ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        if self.heartbeat_interval <= timedelta(0):
            raise ValueError("heartbeat interval must be positive")
        if self.heartbeat_interval >= self.ttl:
            raise ValueError("heartbeat interval must be shorter than lease ttl")


def canonical_workspace_path(repository: Path | str) -> Path:
    """Resolve aliases and normalize platform case for workspace identity."""

    resolved = Path(repository).resolve(strict=True)
    return Path(os.path.normcase(str(resolved)))


def workspace_id_for(repository: Path | str) -> str:
    canonical = canonical_workspace_path(repository)
    return hashlib.sha256(os.fsencode(str(canonical))).hexdigest()


class OwnershipStore(Protocol):
    def claim_execution(
        self,
        execution: Execution,
        *,
        expected_version: int | None = None,
        now: datetime | None = None,
    ) -> Execution: ...

    def renew_execution(
        self, lease_guard: LeaseGuard, *, now: datetime, lease_expires_at: datetime
    ) -> Execution: ...

    def release_execution(self, lease_guard: LeaseGuard, *, now: datetime) -> None: ...

    def assert_execution(self, lease_guard: LeaseGuard, *, now: datetime) -> Execution: ...

    def acquire_workspace_writer(
        self, lease: WorkspaceLease, *, lease_guard: LeaseGuard, now: datetime
    ) -> WorkspaceLease: ...

    def renew_workspace_writer(
        self,
        lease_guard: WorkspaceLeaseGuard,
        *,
        now: datetime,
        lease_expires_at: datetime,
    ) -> WorkspaceLease: ...

    def release_workspace_writer(
        self, lease_guard: WorkspaceLeaseGuard, *, now: datetime
    ) -> None: ...

    def assert_workspace_writer(
        self, lease_guard: WorkspaceLeaseGuard, *, now: datetime
    ) -> WorkspaceLease: ...

    def recovery_commands_for_task(
        self, task_id: str, *, now: datetime
    ) -> list[ManagedCommandIdentity]: ...

    def recovery_commands_for_workspace(
        self, workspace_id: str, *, now: datetime
    ) -> list[ManagedCommandIdentity]: ...

    def finish_managed_command(
        self, identity: ManagedCommandIdentity
    ) -> ManagedCommandIdentity: ...

    def mark_command_recovery_required(
        self, identity: ManagedCommandIdentity, *, cleanup_info: str
    ) -> str: ...

    def append_event(
        self, event: SessionEvent, *, expected_sequence: int | None = None
    ) -> SessionEvent: ...


@dataclass(frozen=True)
class ExecutionOwnership:
    execution: Execution
    workspace_lease: WorkspaceLease | None = None

    @property
    def lease_guard(self) -> LeaseGuard:
        return LeaseGuard(
            execution_id=self.execution.id,
            task_id=self.execution.task_id,
            token=self.execution.lease_token,
            generation=self.execution.generation,
            owner_id=self.execution.owner_id,
        )

    @property
    def workspace_guard(self) -> WorkspaceLeaseGuard | None:
        if self.workspace_lease is None:
            return None
        return WorkspaceLeaseGuard(
            workspace_id=self.workspace_lease.workspace_id,
            execution_id=self.workspace_lease.execution_id,
            token=self.workspace_lease.lease_token,
            generation=self.workspace_lease.generation,
            owner_id=self.workspace_lease.owner_id,
        )


class ExecutionOwnershipManager:
    """Acquire ownership in Session -> Task -> Workspace order."""

    def __init__(
        self,
        store: OwnershipStore,
        *,
        policy: LeasePolicy | None = None,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        token_factory: TokenFactory | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or LeasePolicy()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.id_factory = id_factory or (lambda: str(uuid4()))
        self.token_factory = token_factory or (lambda: uuid4().hex)

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("lease clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    def _token(self, *, different_from: str | None = None) -> str:
        token = self.token_factory()
        if not token or token == different_from:
            raise ValueError("lease token factory must return a new non-empty token")
        return token

    def acquire(
        self,
        *,
        session_id: str,
        task_id: str,
        owner_id: str,
        repository: Path | str,
        expected_version: int | None = None,
        workspace_writer: bool = True,
    ) -> ExecutionOwnership:
        canonical = canonical_workspace_path(repository) if workspace_writer else None
        now = self._now()
        self._reconcile_commands(self.store.recovery_commands_for_task(task_id, now=now))
        execution_token = self._token()
        candidate = Execution(
            id=self.id_factory(),
            session_id=session_id,
            task_id=task_id,
            owner_id=owner_id,
            lease_token=execution_token,
            lease_expires_at=now + self.policy.ttl,
        )
        try:
            execution = self.store.claim_execution(
                candidate,
                expected_version=expected_version,
                now=now,
            )
        except LeaseConflict as exc:
            with suppress(Exception):
                self._record_lease_event(
                    candidate,
                    event_type="lease.contended",
                    scope="session" if exc.resource_id == session_id else "task",
                    resource_id=exc.resource_id,
                    generation=candidate.generation,
                    now=now,
                    recovery_advice="wait for the current owner or retry after lease expiry",
                    holder_id=exc.owner_id,
                )
            raise
        ownership = ExecutionOwnership(execution=execution)
        if not workspace_writer:
            return ownership
        assert canonical is not None
        try:
            workspace_id = workspace_id_for(canonical)
            self._reconcile_commands(
                self.store.recovery_commands_for_workspace(workspace_id, now=now)
            )
            workspace_lease = self.store.acquire_workspace_writer(
                WorkspaceLease(
                    workspace_id=workspace_id,
                    repository_path=str(canonical),
                    session_id=session_id,
                    task_id=task_id,
                    execution_id=execution.id,
                    owner_id=owner_id,
                    lease_token=self._token(different_from=execution_token),
                    generation=1,
                    lease_expires_at=execution.lease_expires_at,
                    acquired_at=now,
                    updated_at=now,
                ),
                lease_guard=ownership.lease_guard,
                now=now,
            )
        except BaseException as exc:
            if isinstance(exc, LeaseConflict):
                with suppress(Exception):
                    self._record_lease_event(
                        execution,
                        event_type="lease.contended",
                        scope="workspace",
                        resource_id=exc.resource_id,
                        generation=execution.generation,
                        now=now,
                        recovery_advice=(
                            "wait for the writer or retry after verified process cleanup"
                        ),
                        holder_id=exc.owner_id,
                    )
            self.store.release_execution(ownership.lease_guard, now=now)
            raise
        return ExecutionOwnership(execution=execution, workspace_lease=workspace_lease)

    def _reconcile_commands(self, commands: list[ManagedCommandIdentity]) -> None:
        for identity in commands:
            try:
                reconciled = reconcile_managed_command(identity)
                self.store.finish_managed_command(reconciled)
            except Exception as exc:
                failed = identity.model_copy(
                    update={
                        "status": ManagedCommandStatus.CLEANUP_FAILED,
                        "cleanup_reason": str(exc)[:256],
                        "updated_at": self._now(),
                    }
                )
                recorded = self.store.finish_managed_command(failed)
                if recorded.status in {
                    ManagedCommandStatus.EXITED,
                    ManagedCommandStatus.TERMINATED,
                }:
                    continue
                task_id = self.store.mark_command_recovery_required(
                    failed, cleanup_info=str(exc)[:256]
                )
                raise RecoveryRequired(task_id, identity.id) from exc

    def renew(self, ownership: ExecutionOwnership) -> ExecutionOwnership:
        now = self._now()
        expires_at = now + self.policy.ttl
        try:
            execution = self.store.renew_execution(
                ownership.lease_guard,
                now=now,
                lease_expires_at=expires_at,
            )
            workspace_lease = ownership.workspace_lease
            workspace_guard = ownership.workspace_guard
            if workspace_lease is not None and workspace_guard is not None:
                workspace_lease = self.store.renew_workspace_writer(
                    workspace_guard,
                    now=now,
                    lease_expires_at=expires_at,
                )
        except BaseException as exc:
            with suppress(Exception):
                self._record_lease_event(
                    ownership.execution,
                    event_type="lease.renew_failed",
                    scope="execution",
                    resource_id=ownership.execution.task_id,
                    generation=ownership.execution.generation,
                    now=now,
                    recovery_advice="stop new actions and verify current ownership",
                    error_type=type(exc).__name__,
                )
                if isinstance(exc, LeaseLost):
                    self._record_lease_event(
                        ownership.execution,
                        event_type="lease.lost",
                        scope="execution",
                        resource_id=exc.resource_id,
                        generation=ownership.execution.generation,
                        now=now,
                        recovery_advice="clean managed commands before attempting takeover",
                    )
            raise
        return ExecutionOwnership(execution=execution, workspace_lease=workspace_lease)

    def assert_owned(self, ownership: ExecutionOwnership) -> None:
        now = self._now()
        try:
            self.store.assert_execution(ownership.lease_guard, now=now)
            workspace_guard = ownership.workspace_guard
            if workspace_guard is not None:
                self.store.assert_workspace_writer(workspace_guard, now=now)
        except LeaseLost as exc:
            with suppress(Exception):
                self._record_lease_event(
                    ownership.execution,
                    event_type="lease.lost",
                    scope=(
                        "workspace"
                        if ownership.workspace_guard is not None
                        and exc.resource_id == ownership.workspace_guard.workspace_id
                        else "execution"
                    ),
                    resource_id=exc.resource_id,
                    generation=ownership.execution.generation,
                    now=now,
                    recovery_advice="clean managed commands before attempting takeover",
                )
            raise

    def release(self, ownership: ExecutionOwnership) -> None:
        now = self._now()
        workspace_guard = ownership.workspace_guard
        if workspace_guard is not None:
            self.store.release_workspace_writer(workspace_guard, now=now)
        self.store.release_execution(ownership.lease_guard, now=now)

    def _record_lease_event(
        self,
        execution: Execution,
        *,
        event_type: str,
        scope: str,
        resource_id: str,
        generation: int,
        now: datetime,
        recovery_advice: str,
        holder_id: str | None = None,
        error_type: str | None = None,
    ) -> None:
        data: dict[str, object] = {
            "execution_id": execution.id,
            "scope": scope,
            "resource_id": resource_id,
            "owner_summary": lease_owner_summary(execution.owner_id),
            "generation": generation,
            "recovery_advice": recovery_advice,
        }
        if holder_id is not None:
            data["holder_summary"] = lease_owner_summary(holder_id)
        if error_type is not None:
            data["error_type"] = error_type
        self.store.append_event(
            SessionEvent(
                id=journal_event_id(
                    event_type,
                    execution.id,
                    f"{scope}:{resource_id}:{generation}:{now.isoformat()}",
                ),
                session_id=execution.session_id,
                task_id=execution.task_id,
                type=event_type,
                data=data,
                timestamp=now,
            )
        )


class LeaseHeartbeat:
    """Renew one ownership claim while synchronous provider/tool work is blocked."""

    def __init__(self, manager: ExecutionOwnershipManager, ownership: ExecutionOwnership) -> None:
        self.manager = manager
        self._ownership = ownership
        self._failure: BaseException | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def ownership(self) -> ExecutionOwnership:
        with self._lock:
            return self._ownership

    @property
    def failure(self) -> BaseException | None:
        with self._lock:
            return self._failure

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("lease heartbeat was already started")
        self._thread = threading.Thread(
            target=self._run,
            name=f"patchloop-lease-{self._ownership.execution.id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def assert_owned(self) -> None:
        with self._lock:
            failure = self._failure
            ownership = self._ownership
        if failure is not None:
            raise failure
        self.manager.assert_owned(ownership)

    def _run(self) -> None:
        interval = self.manager.policy.heartbeat_interval.total_seconds()
        while not self._stop.wait(interval):
            try:
                renewed = self.manager.renew(self.ownership)
            except BaseException as exc:
                with self._lock:
                    self._failure = exc
                return
            with self._lock:
                self._ownership = renewed


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_LEASE_TTL",
    "ExecutionOwnership",
    "ExecutionOwnershipManager",
    "LeaseHeartbeat",
    "LeasePolicy",
    "OwnershipStore",
    "canonical_workspace_path",
    "workspace_id_for",
]
