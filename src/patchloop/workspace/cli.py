"""Non-interactive workspace commands with durable approval retries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import typer

from patchloop.domain import Task, TaskOutcome, TaskRuntimeCondition, TaskStatus
from patchloop.execution.approvals import ApprovalPending
from patchloop.execution.ownership import ExecutionOwnershipManager, LeaseHeartbeat
from patchloop.persistence import SQLiteStore
from patchloop.security import SecretRedactor
from patchloop.workspace.models import VerificationInput, WorkspaceMode
from patchloop.workspace.service import WorkspaceService

workspace_app = typer.Typer(help="Review, verify and manage persistent Git workspaces.")


def prepare_session_workspace(
    store: SQLiteStore,
    session_id: str,
    mode: WorkspaceMode,
    base_revision: str | None,
) -> None:
    """Materialize a Session's selected mode before creating its runtime task."""
    session = store.get_session(session_id)
    handles = [item for item in store.list_workspaces(session_id) if item.status != "closed"]
    if handles:
        if handles[0].mode != mode or handles[0].status != "open":
            raise ValueError("workspace mode differs or workspace requires recovery")
        if handles[0].cleanup_status != "creating":
            return
    if session.active_task_id is None:
        task = store.start_task(
            session.id,
            Task(
                id="workspace-maintenance-" + uuid4().hex,
                goal="workspace maintenance",
                repository=session.workspace_ref,
            ),
            expected_version=session.version,
        )
    else:
        task = store.get_task(session.active_task_id)
        if not task.id.startswith("workspace-maintenance-"):
            raise ValueError("session already has an active task")
    manager = ExecutionOwnershipManager(store)
    ownership = manager.acquire(
        session_id=session.id,
        task_id=task.id,
        owner_id="workspace-cli-" + uuid4().hex,
        repository=session.workspace_ref,
    )
    heartbeat = LeaseHeartbeat(manager, ownership)
    heartbeat.start()
    try:
        service = WorkspaceService(
            store, manager=manager, ownership=ownership, config_version=session.config_version
        )
        service.open(
            session.id, Path(session.workspace_ref), mode=mode, base_revision=base_revision
        )
        current = store.get_task(task.id)
        store.update_task(
            current.model_copy(
                update={
                    "outcome": TaskOutcome.COMPLETED,
                    "status": TaskStatus.COMPLETED,
                    "runtime_condition": TaskRuntimeCondition.ENDED,
                }
            ),
            expected_version=current.version,
            lease_guard=ownership.lease_guard,
        )
    finally:
        heartbeat.stop()
        manager.release(ownership)


def workspace_command(
    context: typer.Context,
    identifier: Annotated[
        str | None, typer.Argument(help="Workspace ID (Session ID for open).")
    ] = None,
    repo: Annotated[Path, typer.Option("--repo")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json/--human")] = True,
    mode: Annotated[WorkspaceMode | None, typer.Option("--mode")] = None,
    base_revision: Annotated[str | None, typer.Option("--base-revision")] = None,
    scope: Annotated[str, typer.Option("--scope")] = "default",
    paths: Annotated[list[str] | None, typer.Option("--path")] = None,
    command: Annotated[list[str] | None, typer.Option("--arg")] = None,
    message: Annotated[str | None, typer.Option("--message")] = None,
    plan_id: Annotated[str | None, typer.Option("--plan-id")] = None,
) -> None:
    """Use --path repeatedly for selectors and --arg repeatedly for a test argument vector."""
    action = context.info_name or "status"
    manager = None
    ownership = None
    heartbeat = None
    output: dict[str, Any] = {"schema_version": "1.0", "status": "ok"}
    exit_code = 0
    try:
        store = SQLiteStore(repo.resolve() / ".patchloop" / "patchloop.db")
        if identifier is None:
            if action != "status":
                raise ValueError("workspace identifier is required")
            output["data"] = [item.model_dump(mode="json") for item in store.list_workspaces()]
        elif action == "show" or (
            action == "status" and store.get_workspace(identifier).status != "open"
        ):
            output["data"] = store.get_workspace(identifier).model_dump(mode="json")
        elif action == "close" and store.get_workspace(identifier).status == "closed":
            output["data"] = {"workspace_id": identifier, "status": "closed"}
        else:
            if action == "open":
                session = store.get_session(identifier)
                root = Path(session.workspace_ref)
                existing = [
                    item
                    for item in store.list_workspaces(session.id)
                    if item.status != "closed" and item.cleanup_status != "creating"
                ]
                if existing:
                    root = existing[0].effective_root
            else:
                handle = store.get_workspace(identifier)
                session = store.get_session(handle.session_id)
                root = handle.effective_root
            if session.active_task_id is None:
                task = store.start_task(
                    session.id,
                    Task(
                        id="workspace-maintenance-" + uuid4().hex,
                        goal="workspace maintenance",
                        repository=str(root),
                    ),
                    expected_version=session.version,
                )
            else:
                task = store.get_task(session.active_task_id)
                if Path(task.repository).resolve() != root.resolve():
                    raise ValueError("active task is bound to another execution root")
            manager = ExecutionOwnershipManager(store)
            ownership = manager.acquire(
                session_id=session.id,
                task_id=task.id,
                owner_id="workspace-cli-" + uuid4().hex,
                repository=root,
            )
            heartbeat = LeaseHeartbeat(manager, ownership)
            heartbeat.start()
            service = WorkspaceService(
                store, manager=manager, ownership=ownership, config_version=session.config_version
            )
            if action == "open":
                output["data"] = service.open(
                    session.id,
                    Path(session.workspace_ref),
                    mode=mode or WorkspaceMode(session.workspace_mode),
                    base_revision=base_revision or session.workspace_base_revision,
                ).model_dump(mode="json")
            elif action == "status":
                output["data"] = service.status(identifier)
            elif action == "diff":
                output["data"] = service.diff(identifier, scope)
            elif action == "accept":
                service.accept(identifier, paths or [])
                output["data"] = {"accepted": paths or []}
            elif action == "revert":
                output["data"] = service.revert(identifier, paths or [])
            elif action == "verify":
                output["data"] = service.verify(
                    identifier, VerificationInput(command=command or [])
                ).model_dump(mode="json")
            elif action == "commit":
                if plan_id is not None:
                    output["data"] = service.commit(identifier, plan_id)
                elif message is not None:
                    output["data"] = service.prepare_commit(identifier, message).model_dump(
                        mode="json"
                    )
                else:
                    raise ValueError("provide --message to prepare, or --plan-id to commit")
            elif action == "close":
                service.close(identifier)
                output["data"] = {"workspace_id": identifier, "status": "closed"}
            if task.id.startswith("workspace-maintenance-"):
                current = store.get_task(task.id)
                store.update_task(
                    current.model_copy(
                        update={
                            "outcome": TaskOutcome.COMPLETED,
                            "status": TaskStatus.COMPLETED,
                            "runtime_condition": TaskRuntimeCondition.ENDED,
                        }
                    ),
                    expected_version=current.version,
                    lease_guard=ownership.lease_guard,
                )
    except ApprovalPending as exc:
        output.update(
            status="approval_required",
            approval_id=exc.approval.id,
            next_command=f"patchloop approval --repo {repo} approve {exc.approval.id}",
        )
        exit_code = 3
    except (RuntimeError, ValueError, KeyError, OSError) as exc:
        output.update(
            status="error",
            error={
                "code": getattr(exc, "code", type(exc).__name__),
                "message": SecretRedactor().redact_text(str(exc)),
            },
        )
        exit_code = 1
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        if manager is not None and ownership is not None:
            manager.release(ownership)
    typer.echo(
        json.dumps(output, ensure_ascii=False, default=str)
        if json_output
        else json.dumps(output, ensure_ascii=False, indent=2, default=str)
    )
    if exit_code:
        raise typer.Exit(exit_code)


for _name in ("open", "status", "show", "diff", "accept", "revert", "verify", "commit", "close"):
    workspace_app.command(_name)(workspace_command)
