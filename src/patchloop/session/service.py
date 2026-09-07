"""Application service for long-lived PatchLoop sessions."""

from __future__ import annotations

import builtins
import time
from typing import TYPE_CHECKING, Any, Protocol, cast

from patchloop.domain import (
    Task,
    TaskBudget,
    TaskExecutionConfig,
    TaskRuntimeCondition,
    TaskStatus,
)
from patchloop.execution.models import ControlKind, ControlRequest, ControlStatus, Execution
from patchloop.session.models import Session, Turn, TurnRole

if TYPE_CHECKING:
    from patchloop.persistence_contracts import Store


class SessionRuntime(Protocol):
    """Legacy Runtime surface used until Session-aware advance owns execution."""

    def run(self, task: Task) -> Task: ...

    def resume(self, task: Task, checkpoint: Any) -> Task: ...


class SessionService:
    """Coordinate Session commands without exposing persistence details."""

    def __init__(self, store: Store, runtime: SessionRuntime | None = None) -> None:
        self.store = store
        self.runtime = runtime

    def create(
        self,
        workspace_ref: str,
        *,
        session_id: str | None = None,
        config_version: str = "1",
    ) -> Session:
        values: dict[str, object] = {
            "workspace_ref": workspace_ref,
            "config_version": config_version,
        }
        if session_id is not None:
            values["id"] = session_id
        return self.store.create_session(Session.model_validate(values))

    def list(self, workspace_ref: str | None = None) -> builtins.list[Session]:
        return self.store.list_sessions(workspace_ref)

    def get(self, session_id: str) -> Session:
        return self.store.get_session(session_id)

    def turns(self, session_id: str, *, after_sequence: int = 0) -> builtins.list[Turn]:
        """Return persisted conversation turns in Session sequence order."""

        return self.store.list_turns(session_id, after_sequence=after_sequence)

    def active_task(self, session_id: str) -> Task | None:
        """Return the active Task without exposing Store access to callers."""

        session = self.store.get_session(session_id)
        if session.active_task_id is None:
            return None
        return self.store.get_task(session.active_task_id)

    def task(self, task_id: str) -> Task:
        """Return a Task through the shared application-service boundary."""

        return self.store.get_task(task_id)

    def cancel_task(self, task_id: str) -> Task:
        """Request cancellation through the task-compatible service surface."""

        cancel = getattr(self.store, "cancel_task", None)
        if not callable(cancel):
            raise RuntimeError("Session store does not support task cancellation")
        return cast(Task, cancel(task_id))

    def control(self, request_id: str) -> ControlRequest:
        """Return a persisted control request for cleanup/status reporting."""

        return self.store.get_control_request(request_id)

    def executions(self, task_id: str) -> builtins.list[Execution]:
        """Return execution attempts in generation order."""

        return self.store.list_executions(task_id)

    def checkpoint(self, task_id: str) -> Any:
        """Return the version-adapted checkpoint projection for presentation."""

        return self._load_runtime_checkpoint(task_id)

    def wait_for_control(
        self,
        request_id: str,
        *,
        timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.05,
    ) -> ControlRequest:
        """Wait briefly for cleanup to settle, returning the latest durable state."""

        if timeout_seconds < 0 or poll_interval_seconds <= 0:
            raise ValueError("control wait timing must be positive")
        deadline = time.monotonic() + timeout_seconds
        while True:
            request = self.store.get_control_request(request_id)
            if request.status in {ControlStatus.SETTLED, ControlStatus.CLEANUP_FAILED}:
                return request
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return request
            time.sleep(min(poll_interval_seconds, remaining))

    def append_message(
        self,
        session_id: str,
        content: str,
        *,
        client_submission_id: str | None = None,
        resource_refs: builtins.list[str] | None = None,
    ) -> Turn:
        """Persist a user message and return only the committed Turn."""

        return self.store.append_turn(
            Turn(
                session_id=session_id,
                role=TurnRole.USER,
                content=content,
                resource_refs=list(resource_refs or ()),
                client_submission_id=client_submission_id,
            )
        )

    def start_task(
        self,
        session_id: str,
        goal: str | Task,
        *,
        task_id: str | None = None,
        budget: TaskBudget | None = None,
        execution: TaskExecutionConfig | None = None,
    ) -> Task:
        session = self.store.get_session(session_id)
        if isinstance(goal, Task):
            if task_id is not None or budget is not None or execution is not None:
                raise ValueError("Task overrides cannot be combined with a Task instance")
            if goal.session_id not in {None, session.id}:
                raise ValueError("task belongs to a different session")
            if goal.repository != session.workspace_ref:
                raise ValueError("task repository must match the Session workspace")
            return self.store.start_task(
                session.id,
                goal,
                expected_version=session.version,
            )
        values: dict[str, object] = {
            "goal": goal,
            "repository": session.workspace_ref,
        }
        if task_id is not None:
            values["id"] = task_id
        if budget is not None:
            values["budget"] = budget
        if execution is not None:
            values["execution"] = execution
        task = Task.model_validate(values)
        return self.store.start_task(session.id, task, expected_version=session.version)

    def request_pause(
        self,
        session_id: str,
        *,
        execution_id: str | None = None,
        request_id: str | None = None,
    ) -> ControlRequest:
        return self._request_control(
            session_id,
            ControlKind.PAUSE,
            execution_id=execution_id,
            request_id=request_id,
        )

    def request_cancel(
        self,
        session_id: str,
        *,
        execution_id: str | None = None,
        request_id: str | None = None,
    ) -> ControlRequest:
        return self._request_control(
            session_id,
            ControlKind.CANCEL,
            execution_id=execution_id,
            request_id=request_id,
        )

    def resume(self, session_id: str) -> Task:
        session = self.store.get_session(session_id)
        task = self._active_task(session)
        if self.runtime is None:
            raise RuntimeError("Session runtime is not configured")
        if task.runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED:
            return task
        if task.status is TaskStatus.CREATED:
            return self.runtime.run(task)
        checkpoint = self._load_runtime_checkpoint(task.id)
        return self.runtime.resume(task, checkpoint)

    def resume_task(self, task_id: str) -> Task:
        """Resume either a Session Task or a legacy Task through one service."""

        task = self.store.get_task(task_id)
        if task.session_id is not None:
            return self.resume(task.session_id)
        if self.runtime is None:
            raise RuntimeError("Session runtime is not configured")
        checkpoint = self._load_runtime_checkpoint(task.id)
        return self.runtime.resume(task, checkpoint)

    def close(self, session_id: str) -> Session:
        session = self.store.get_session(session_id)
        return self.store.close_session(session.id, expected_version=session.version)

    def _request_control(
        self,
        session_id: str,
        kind: ControlKind,
        *,
        execution_id: str | None,
        request_id: str | None,
    ) -> ControlRequest:
        session = self.store.get_session(session_id)
        task = self._active_task(session)
        if execution_id is not None:
            self._validate_execution(session, task, execution_id)
        values: dict[str, object] = {
            "task_id": task.id,
            "execution_id": execution_id,
            "kind": kind,
        }
        if request_id is not None:
            values["id"] = request_id
        request = ControlRequest.model_validate(values)
        if kind is ControlKind.PAUSE:
            return self.store.request_pause(request, expected_version=task.version)
        return self.store.request_cancel(request, expected_version=task.version)

    def _active_task(self, session: Session) -> Task:
        if session.active_task_id is None:
            raise ValueError(f"session {session.id} has no active task")
        return self.store.get_task(session.active_task_id)

    def _load_runtime_checkpoint(self, task_id: str) -> Any:
        legacy_loader = getattr(self.store, "get_checkpoint", None)
        if callable(legacy_loader):
            return legacy_loader(task_id)
        return self.store.get_session_checkpoint(task_id)

    def _validate_execution(self, session: Session, task: Task, execution_id: str) -> None:
        loader = getattr(self.store, "get_execution", None)
        if not callable(loader):
            raise ValueError("Session store cannot validate an execution reference")
        execution = loader(execution_id)
        if execution.session_id != session.id or execution.task_id != task.id:
            raise ValueError("execution does not belong to the active Session task")


__all__ = ["SessionRuntime", "SessionService"]
