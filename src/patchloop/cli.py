"""PatchLoop command-line interface."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
from functools import cached_property
from pathlib import Path
from typing import Annotated, Never, Optional, cast
from uuid import uuid4

import typer

from patchloop.context import ContextDebug
from patchloop.domain import (
    DEFAULT_PROMPT_CACHE_LAYOUT,
    AppendOnlyOptimizationVersion,
    PromptCacheLayout,
    Task,
    TaskBudget,
    TaskExecutionConfig,
    TaskOutcome,
    TaskStatus,
)
from patchloop.evaluation import (
    AopEvidenceFile,
    AopOptimizationEvidence,
    CacheAcceptanceEvaluator,
    CacheBenchmarkRunner,
    CacheEvaluationReport,
    CacheEvaluationVariant,
    CacheRolloutPolicy,
    CodingBenchmarkRunner,
    EvaluationRunner,
    EvaluationVariant,
    ExperimentRunner,
    MemoryAblationRunner,
    MemoryBenchmarkMode,
    MemoryBenchmarkRunner,
    MemoryBenchmarkVariant,
    MemoryQualityEvidence,
    RealProviderCacheCollector,
    RetrievalBaseline,
    aop_source_fingerprint,
    append_only_overhead_fixture_fingerprint,
    load_coding_manifest,
    load_evaluation_manifest,
    load_memory_manifest,
    summarize_cache_run,
)
from patchloop.events import EventLogger, lease_owner_summary
from patchloop.execution.approvals import ApprovalPending, ApprovalService
from patchloop.execution.models import Approval, Effect
from patchloop.execution.policy import ApprovalScopeKind
from patchloop.execution.policy_config import load_policy_configuration
from patchloop.execution.recovery import RecoveryService
from patchloop.intelligence import (
    RepositoryIndexer,
    RepositorySearch,
    RepositorySnapshot,
    evaluate_retrieval,
    load_retrieval_tasks,
)
from patchloop.memory import MemoryKind, MemoryQuery, MemoryStatus, MemoryStoreError
from patchloop.observability import TaskMetrics, TaskReplay
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import (
    ApprovalConflict,
    ContractError,
    InputRevisionConflict,
    LeaseConflict,
    LeaseLost,
    RecoveryRequired,
    StaleVersion,
    SubmissionConflict,
)
from patchloop.providers import (
    CredentialResolver,
    DeepSeekProvider,
    ModelMessage,
    ModelProvider,
    ProfileResolver,
    ProviderBinding,
    ProviderError,
    ProviderEvent,
    ProviderEventObserver,
    ProviderFactory,
    ProviderRequest,
    ProviderRequestPurpose,
)
from patchloop.providers.base import ProviderGateway as ProviderGatewayPort
from patchloop.providers.transport import HttpxTransport
from patchloop.runtime import AgentRuntime
from patchloop.sandbox import (
    DockerSandbox,
    LocalProcessSandbox,
    create_command_sandbox,
)
from patchloop.security import RiskLevel, SecretRedactor
from patchloop.session import SessionService
from patchloop.storage import ArtifactStore, TaskNotFoundError
from patchloop.tools import (
    ApplyPatchTool,
    CreateFileTool,
    GetDiffTool,
    ListFilesTool,
    PermissionLevel,
    ReadFileTool,
    ReplaceTextTool,
    RunCommandTool,
    RunTestsTool,
    SearchCodeTool,
    SearchTextTool,
    ToolContext,
    ToolGateway,
    ToolPolicy,
    UpdatePlanTool,
    WriteFileTool,
)
from patchloop.tools.base import Tool
from patchloop.workspace.cli import prepare_session_workspace, workspace_app
from patchloop.workspace.models import WorkspaceMode
from patchloop.workspace.service import WorkspaceService

app = typer.Typer(help="PatchLoop local-first coding agent runtime.", no_args_is_help=True)
task_app = typer.Typer(help="Create and inspect local tasks.", no_args_is_help=True)
session_app = typer.Typer(help="Manage persistent PatchLoop sessions.", no_args_is_help=True)
approval_app = typer.Typer(help="Inspect and decide persistent approvals.", no_args_is_help=True)
grant_app = typer.Typer(help="Inspect and revoke exact scope grants.", no_args_is_help=True)
policy_app = typer.Typer(help="Inspect policy rules and execution evidence.", no_args_is_help=True)
provider_app = typer.Typer(help="Inspect and validate provider profiles.", no_args_is_help=True)
app.add_typer(task_app, name="task")
app.add_typer(session_app, name="session")
app.add_typer(workspace_app, name="workspace")
app.add_typer(approval_app, name="approval")
approval_app.add_typer(grant_app, name="grant")
app.add_typer(policy_app, name="policy")
app.add_typer(provider_app, name="provider")

CLI_SCHEMA_VERSION = "1.0"
type RuntimeProvider = ModelProvider | ProviderGatewayPort


class CliExitCode(IntEnum):
    SUCCESS = 0
    EXECUTION_FAILED = 1
    USAGE_ERROR = 2
    WAITING_FOR_APPROVAL = 10
    PAUSED = 11
    CONFLICT = 12
    RECOVERY_REQUIRED = 13
    CANCELLED = 14


class InvalidRepository(ValueError):
    """The selected workspace path is missing or is not a directory."""


class CliUsageError(ValueError):
    """A command configuration error that maps to the stable usage exit code."""


@dataclass(frozen=True)
class WorkspaceServices:
    """Workspace-local application services available to CLI commands."""

    repository: Path
    json_output: bool = True

    @cached_property
    def workspace(self) -> WorkspaceService:
        return WorkspaceService(self.store)

    @cached_property
    def store(self) -> SQLiteStore:
        return _sqlite_store(self.repository)

    @cached_property
    def session(self) -> SessionService:
        return SessionService(self.store)

    @cached_property
    def approval(self) -> ApprovalService:
        return ApprovalService(self.store)

    @cached_property
    def recovery(self) -> RecoveryService:
        return RecoveryService(self.store)


def _state_dir(repository: Path) -> Path:
    return repository.resolve() / ".patchloop"


def _sqlite_store(repository: Path) -> SQLiteStore:
    return SQLiteStore(_state_dir(repository) / "patchloop.db")


def _workspace_services(repository: Path, *, json_output: bool = True) -> WorkspaceServices:
    """Resolve the service boundary for a repository selected by the CLI."""

    resolved = repository.resolve()
    if not resolved.exists():
        raise InvalidRepository(f"repository does not exist: {resolved}")
    if not resolved.is_dir():
        raise InvalidRepository(f"repository is not a directory: {resolved}")
    return WorkspaceServices(repository=resolved, json_output=json_output)


def _set_workspace_context(context: typer.Context, repository: Path, *, json_output: bool) -> None:
    context.obj = _workspace_services(repository, json_output=json_output)


@session_app.callback()
def session_group(
    context: typer.Context,
    repo: Annotated[
        Path,
        typer.Option(resolve_path=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json/--human")] = True,
) -> None:
    """Resolve the workspace used by all Session commands."""

    try:
        _set_workspace_context(context, repo, json_output=json_output)
    except InvalidRepository as exc:
        _command_error(exc)


@approval_app.callback()
@policy_app.callback()
def approval_group(
    context: typer.Context,
    repo: Annotated[
        Path,
        typer.Option(resolve_path=True),
    ] = Path("."),
    json_output: Annotated[bool, typer.Option("--json/--human")] = True,
) -> None:
    """Resolve the workspace used by all Approval commands."""

    try:
        _set_workspace_context(context, repo, json_output=json_output)
    except InvalidRepository as exc:
        _command_error(exc)


def _services_from_context(context: typer.Context) -> WorkspaceServices:
    services = context.obj
    if not isinstance(services, WorkspaceServices):
        raise RuntimeError("workspace services are not configured")
    return services


def _echo_json(value: object) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def _echo_json_line(value: object) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    typer.echo(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _command_error(exc: Exception) -> Never:
    message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
    conflict_types = (
        ApprovalConflict,
        InputRevisionConflict,
        LeaseConflict,
        LeaseLost,
        StaleVersion,
        SubmissionConflict,
    )
    if isinstance(exc, conflict_types):
        category = "conflict"
        exit_code = CliExitCode.CONFLICT
    elif isinstance(exc, RecoveryRequired):
        category = "recovery_required"
        exit_code = CliExitCode.RECOVERY_REQUIRED
    elif isinstance(exc, (KeyError, TaskNotFoundError)):
        category = "not_found"
        exit_code = CliExitCode.EXECUTION_FAILED
    elif isinstance(exc, CliUsageError):
        category = "usage_error"
        exit_code = CliExitCode.USAGE_ERROR
    elif isinstance(exc, ProviderError):
        category = exc.kind.value
        exit_code = (
            CliExitCode.USAGE_ERROR
            if exc.kind.value in {"configuration", "pricing_required"}
            else CliExitCode.EXECUTION_FAILED
        )
    elif isinstance(exc, InvalidRepository):
        category = "invalid_repository"
        exit_code = CliExitCode.EXECUTION_FAILED
    elif isinstance(exc, ValueError):
        category = "invalid_state"
        exit_code = CliExitCode.EXECUTION_FAILED
    else:
        category = "execution_failed"
        exit_code = CliExitCode.EXECUTION_FAILED
    _echo_json(
        {
            "schema_version": CLI_SCHEMA_VERSION,
            "session_id": None,
            "task_id": None,
            "execution_id": None,
            "status": "error",
            "latest_sequence": 0,
            "pending_approvals": [],
            "recovery_items": [],
            "error_category": category,
            "error": {
                "category": category,
                "message": str(message),
                "details": getattr(exc, "details", {}),
            },
            "next_command": None,
            "next_commands": [],
        }
    )
    raise typer.Exit(code=int(exit_code)) from None


def _parse_append_only_optimization(
    value: str,
    layout: PromptCacheLayout,
) -> AppendOnlyOptimizationVersion:
    if value not in {"baseline_v1", "balanced_v1"}:
        raise CliUsageError("append_only_optimization must be baseline_v1 or balanced_v1")
    optimization = cast(AppendOnlyOptimizationVersion, value)
    if optimization == "balanced_v1" and layout is not PromptCacheLayout.APPEND_ONLY:
        raise CliUsageError("balanced_v1 optimization requires append_only prompt layout")
    return optimization


def _provider_from_env() -> RuntimeProvider:
    """Resolve the legacy environment entry point through the profile gateway."""

    try:
        binding = ProfileResolver().resolve_legacy_environment()
        credential = CredentialResolver().resolve(binding)
        transport = HttpxTransport(
            binding.base_url,
            credential=credential,
            config=binding.transport,
        )
        return ProviderFactory().create(binding, transport)
    except (ProviderError, ValueError) as exc:
        raise CliUsageError(str(exc)) from exc


def _provider_binding(provider: RuntimeProvider) -> ProviderBinding | None:
    gateway = getattr(provider, "gateway", provider)
    binding = getattr(gateway, "binding", None)
    return binding if isinstance(binding, ProviderBinding) else None


def _configured_provider(
    profile: str | None,
    *,
    model: str | None = None,
    config_path: Path | None = None,
    env_file: Path | None = None,
) -> tuple[RuntimeProvider, ProviderBinding]:
    binding = ProfileResolver().resolve(profile, model, config_path, env_file)
    credential = CredentialResolver().resolve(binding, env_file=env_file)
    transport = HttpxTransport(
        binding.base_url,
        credential=credential,
        config=binding.transport,
    )
    return ProviderFactory().create(binding, transport), binding


def _selected_provider(
    profile: str | None,
    *,
    model: str | None = None,
    config_path: Path | None = None,
    env_file: Path | None = None,
) -> tuple[RuntimeProvider, ProviderBinding | None]:
    if profile is None and model is None and config_path is None and env_file is None:
        provider = _provider_from_env()
        return provider, _provider_binding(provider)
    return _configured_provider(
        profile,
        model=model,
        config_path=config_path,
        env_file=env_file,
    )


def _session_runtime_service(
    services: WorkspaceServices,
    task: Task,
    *,
    provider: RuntimeProvider | None = None,
    provider_event_observer: ProviderEventObserver | None = None,
) -> SessionService:
    policy = _tool_policy(task)
    if provider is None:
        binding = task.execution.provider
        provider = (
            ProviderFactory().create(binding)
            if binding is not None and binding.profile_id != "legacy"
            else _provider_from_env()
        )
    sandbox = create_command_sandbox(task.execution)
    trace = EventLogger(_state_dir(services.repository) / "traces" / f"{task.id}.jsonl")
    gateway = ToolGateway(
        ToolContext(Path(task.repository), sandbox),
        _all_tools(),
        trace,
        policy,
    )
    runtime = AgentRuntime(
        provider,
        gateway,
        trace,
        services.store,
        provider_event_observer=provider_event_observer,
    )
    return SessionService(services.store, runtime)


class _ProviderEventWriter:
    """Emit safe JSONL lifecycle events without exposing partial secret fragments."""

    def __init__(self) -> None:
        self.redactor = SecretRedactor()
        self._text: list[str] = []
        self._last_text_event: ProviderEvent | None = None

    def __call__(self, event: ProviderEvent) -> None:
        event_type = event.type.value
        if event_type == "text_delta":
            self._text.append(event.delta or "")
            self._last_text_event = event
            return
        self.flush()
        payload: dict[str, object] = {
            "version": CLI_SCHEMA_VERSION,
            "type": "reasoning_status" if event_type == "reasoning_delta" else event_type,
            "request_id": event.request_id,
            "attempt_id": event.attempt_id,
            "sequence": event.sequence,
        }
        usage = event.usage
        if usage is not None:
            payload["usage"] = usage.model_dump(mode="json")
        safe_message = event.safe_message
        if safe_message:
            payload["safe_message"] = self.redactor.redact_text(str(safe_message))
        _echo_json_line(payload)

    def flush(self) -> None:
        if not self._text or self._last_text_event is None:
            return
        event = self._last_text_event
        _echo_json_line(
            {
                "version": CLI_SCHEMA_VERSION,
                "type": "text_delta",
                "request_id": event.request_id,
                "attempt_id": event.attempt_id,
                "sequence": event.sequence,
                "delta": self.redactor.redact_text("".join(self._text)),
            }
        )
        self._text.clear()
        self._last_text_event = None


class _HumanProviderWriter:
    """Render safe lifecycle progress while buffering text across chunk boundaries."""

    def __init__(self) -> None:
        self.redactor = SecretRedactor()
        self._text: list[str] = []
        self._reasoning_announced = False

    def __call__(self, event: ProviderEvent) -> None:
        if event.type.value == "text_delta":
            self._text.append(event.delta or "")
        elif event.type.value == "reasoning_delta" and not self._reasoning_announced:
            typer.echo("[provider] reasoning", err=True)
            self._reasoning_announced = True
        elif event.type.value in {"request_started", "attempt_started", "request_failed"}:
            typer.echo(f"[provider] {event.type.value}", err=True)
        elif event.type.value == "response_completed":
            self.flush()

    def flush(self) -> None:
        if self._text:
            typer.echo(self.redactor.redact_text("".join(self._text)))
            self._text.clear()


def _echo_human_task(task: Task) -> None:
    typer.echo(f"status: {_effective_task_status(task)}")
    if task.result:
        typer.echo(task.result)
    elif task.report is not None:
        typer.echo(task.report.summary)


def _tool_policy(task: Task) -> ToolPolicy:
    permissions = frozenset(PermissionLevel(value) for value in task.execution.allowed_permissions)
    configuration = load_policy_configuration(Path(task.repository), session_id=task.session_id)
    policy = ToolPolicy(
        permissions,
        approval_threshold=RiskLevel.MEDIUM,
        approval_handler=None,
        policy_extension=configuration.enabled,
        configuration_fingerprint=configuration.fingerprint,
    )
    policy.rules = configuration.rules_for(policy.version)
    return policy


def _pause_interrupted_session(services: WorkspaceServices, session_id: str) -> None:
    """Persist Ctrl+C as pause and drive cleanup to a durable boundary when possible."""

    try:
        request = services.session.request_pause(session_id)
        task = services.session.active_task(session_id)
        if task is not None:
            with suppress(
                ContractError,
                OSError,
                RuntimeError,
                TaskNotFoundError,
                ValueError,
            ):
                _session_runtime_service(services, task).resume(session_id)
        current = services.session.wait_for_control(request.id)
        current_task = services.session.active_task(session_id)
        _echo_json(
            _command_payload(
                services,
                data={
                    "interrupted": True,
                    "control": current.model_dump(mode="json"),
                    "task": (
                        None if current_task is None else current_task.model_dump(mode="json")
                    ),
                },
                session_id=session_id,
                task=current_task,
            )
        )
    except (ContractError, KeyError, TaskNotFoundError, ValueError) as exc:
        _echo_json(
            {
                "schema_version": CLI_SCHEMA_VERSION,
                "session_id": session_id,
                "task_id": None,
                "execution_id": None,
                "status": "error",
                "latest_sequence": 0,
                "pending_approvals": [],
                "recovery_items": [],
                "error_category": "pause_request_failed",
                "error": {
                    "category": "pause_request_failed",
                    "message": str(exc),
                    "details": getattr(exc, "details", {}),
                },
                "next_command": None,
                "next_commands": [],
            }
        )
    raise typer.Exit(code=130)


def _task_diff_projection(services: WorkspaceServices, task: Task) -> str:
    if task.report is not None:
        return task.report.diff or "No changes."
    try:
        checkpoint = services.session.checkpoint(task.id)
    except (KeyError, TaskNotFoundError):
        return "No changes."
    context = ToolContext(Path(task.repository))
    context.changes.restore(checkpoint.change_snapshot)
    return context.changes.diff() or "No changes."


def _activity_projection(services: WorkspaceServices, task: Task) -> list[dict[str, object]]:
    events = EventLogger(_state_dir(services.repository) / "traces" / f"{task.id}.jsonl").read()
    replay = TaskReplay.from_events(task.id, events)
    visible = {
        "model.completed",
        "provider.request.started",
        "provider.attempt.started",
        "provider.paused",
        "tool.completed",
        "tool.replayed",
        "approval.waiting",
        "effect.recovery_required",
        "task.paused",
        "task.completed",
        "task.failed",
        "task.cancelled",
    }
    return [
        {
            "sequence": frame.sequence,
            "type": frame.type,
            "step": frame.step,
            "summary": frame.summary,
            "data": frame.data,
        }
        for frame in replay.frames
        if frame.type in visible
    ]


def _execution_projection(services: WorkspaceServices, task: Task) -> dict[str, object] | None:
    executions = services.session.executions(task.id)
    if not executions:
        return None
    execution = executions[-1]
    active = execution.status.value not in {
        "completed",
        "failed",
        "cancelled",
        "released",
    } and execution.lease_expires_at > datetime.now(UTC)
    return {
        "id": execution.id,
        "status": execution.status.value,
        "generation": execution.generation,
        "prompt_cache_layout": task.execution.prompt_cache_layout.value,
        "append_only_optimization": task.execution.append_only_optimization,
        "owner": lease_owner_summary(execution.owner_id),
        "lease_expires_at": execution.lease_expires_at.isoformat(),
        "active": active,
        "attempts": len(executions),
    }


def _next_commands(
    services: WorkspaceServices,
    session_id: str,
    task: Task | None,
    approvals: list[Approval],
    recoveries: list[Effect],
    *,
    session_open: bool = True,
) -> list[str]:
    repository = json.dumps(str(services.repository), ensure_ascii=False)
    prefix = f"patchloop session --repo {repository}"
    if task is None:
        return [f'{prefix} start {session_id} "<goal>"'] if session_open else []
    inspection_commands = [
        f"patchloop status {task.id} --repo {repository}",
        f"patchloop diff {task.id} --repo {repository}",
        f"patchloop replay {task.id} --repo {repository}",
    ]
    if task.outcome.value != "active":
        return inspection_commands
    commands = [
        f'{prefix} send {session_id} "<message>"',
        f"{prefix} pause {session_id}",
        f"{prefix} cancel {session_id}",
        *inspection_commands,
    ]
    if task.runtime_condition.value in {"paused", "idle", "waiting_for_approval"}:
        commands.insert(0, f"{prefix} resume {session_id}")
    for approval in approvals:
        if approval.status.value != "pending":
            continue
        approval_prefix = f"patchloop approval --repo {repository} decide {approval.id}"
        commands.extend([f"{approval_prefix} --approve", f"{approval_prefix} --deny"])
    for effect in recoveries:
        commands.append(f"{prefix} recover {session_id} --effect-id {effect.id}")
    return commands


def _task_presentation(
    services: WorkspaceServices,
    session_id: str,
    task: Task,
    approvals: list[Approval],
    recoveries: list[Effect],
) -> dict[str, object]:
    payload = task.model_dump(mode="json")
    activity = _activity_projection(services, task)
    current_plan = task.plan
    if current_plan is None:
        with suppress(KeyError, TaskNotFoundError):
            current_plan = services.session.checkpoint(task.id).plan
    plan_changes = [
        item
        for item in activity
        if item["type"] in {"tool.completed", "tool.replayed"}
        and isinstance(item["data"], dict)
        and isinstance(item["data"].get("call"), dict)
        and item["data"]["call"].get("name") == "update_plan"
    ]
    test_results: list[object] = (
        []
        if task.report is None
        else [item.model_dump(mode="json") for item in task.report.validations]
    )
    if not test_results:
        test_results = [
            item["data"]["result"]
            for item in activity
            if item["type"] in {"tool.completed", "tool.replayed"}
            and isinstance(item["data"], dict)
            and isinstance(item["data"].get("call"), dict)
            and item["data"]["call"].get("name") in {"run_tests", "run_command"}
            and isinstance(item["data"].get("result"), dict)
        ]
    payload.update(
        {
            "workspace": str(services.repository),
            "execution": _execution_projection(services, task),
            "activity": activity,
            "plan": (None if current_plan is None else current_plan.model_dump(mode="json")),
            "plan_changes": plan_changes,
            "test_results": test_results,
            "diff": _task_diff_projection(services, task),
            "next_commands": _next_commands(
                services,
                session_id,
                task,
                approvals,
                recoveries,
            ),
        }
    )
    return payload


def _effective_task_status(task: Task) -> str:
    condition = task.runtime_condition.value
    if condition in {"waiting_for_approval", "pausing", "paused", "recovery_required"}:
        return condition
    return task.outcome.value if task.outcome.value != "active" else condition


def _task_exit_code(task: Task) -> CliExitCode:
    return {
        "waiting_for_approval": CliExitCode.WAITING_FOR_APPROVAL,
        "pausing": CliExitCode.PAUSED,
        "paused": CliExitCode.PAUSED,
        "recovery_required": CliExitCode.RECOVERY_REQUIRED,
        "failed": CliExitCode.EXECUTION_FAILED,
        "cancelled": CliExitCode.CANCELLED,
    }.get(_effective_task_status(task), CliExitCode.SUCCESS)


def _approval_projection(services: WorkspaceServices, approval: Approval) -> dict[str, object]:
    effect = services.store.get_effect(approval.effect_id)
    evaluation = effect.policy_evaluation
    return {
        **approval.model_dump(mode="json"),
        "action": approval.action_summary,
        "resources": approval.resource_summary,
        "matched_rule_ids": [] if evaluation is None else list(evaluation.matched_rule_ids),
        "matched_grant_id": effect.consumed_grant_id
        or (None if evaluation is None else evaluation.matched_grant_id),
        "descriptor": None
        if effect.action_descriptor is None
        else effect.action_descriptor.model_dump(mode="json"),
    }


def _command_payload(
    services: WorkspaceServices,
    *,
    data: dict[str, object] | None = None,
    session_id: str | None = None,
    task: Task | None = None,
    approvals: list[Approval] | None = None,
    recoveries: list[Effect] | None = None,
    status: str = "ok",
    next_commands: list[str] | None = None,
) -> dict[str, object]:
    session = None
    if session_id is not None:
        with suppress(KeyError):
            session = services.session.get(session_id)
    approvals = list(approvals or ())
    recoveries = list(recoveries or ())
    if task is not None:
        status = _effective_task_status(task)
        if not approvals:
            approvals = services.approval.list(task.id)
        if not recoveries:
            recoveries = services.recovery.pending(task.id)
    execution = None if task is None else _execution_projection(services, task)
    commands = list(next_commands or ())
    if not commands and session_id is not None:
        commands = _next_commands(
            services,
            session_id,
            task,
            approvals,
            recoveries,
            session_open=session is None or session.status.value == "open",
        )
    payload = dict(data or {})
    payload.update(
        {
            "schema_version": CLI_SCHEMA_VERSION,
            "session_id": session_id,
            "task_id": None if task is None else task.id,
            "execution_id": None if execution is None else execution["id"],
            "status": status,
            "latest_sequence": 0 if session is None else session.event_sequence,
            "pending_approvals": [
                _approval_projection(services, approval)
                for approval in approvals
                if approval.status.value == "pending"
            ],
            "recovery_items": [
                {
                    "id": effect.id,
                    "tool_name": effect.tool_name,
                    "action_kind": effect.action_kind,
                    "evidence": effect.reconciliation_evidence,
                }
                for effect in recoveries
            ],
            "error_category": None,
            "error": None,
            "next_command": commands[0] if commands else None,
            "next_commands": commands,
        }
    )
    return payload


def _echo_task_result(
    services: WorkspaceServices,
    session_id: str,
    task: Task,
    *,
    exit_for_state: bool = False,
) -> None:
    approvals = services.approval.list(task.id)
    recoveries = services.recovery.pending(task.id)
    presentation = _task_presentation(services, session_id, task, approvals, recoveries)
    _echo_json(
        _command_payload(
            services,
            data=presentation,
            session_id=session_id,
            task=task,
            approvals=approvals,
            recoveries=recoveries,
            next_commands=cast(list[str], presentation["next_commands"]),
        )
    )
    if exit_for_state and (exit_code := _task_exit_code(task)) is not CliExitCode.SUCCESS:
        raise typer.Exit(code=int(exit_code))


def _provider_projection(binding: ProviderBinding) -> dict[str, object]:
    payload = binding.model_dump(mode="json")
    payload["credential_configured"] = binding.auth.value == "none" or (
        binding.credential_env is not None and bool(os.environ.get(binding.credential_env))
    )
    payload["pricing_status"] = "configured" if binding.pricing is not None else "missing"
    return payload


@provider_app.command("list")
def list_providers(
    provider_config: Annotated[Path | None, typer.Option("--provider-config")] = None,
) -> None:
    """List locally configured provider profiles without connecting."""

    try:
        bindings = ProfileResolver().list_bindings(provider_config)
    except ProviderError as exc:
        _command_error(exc)
    _echo_json(
        {
            "schema_version": CLI_SCHEMA_VERSION,
            "items": [_provider_projection(binding) for binding in bindings],
        }
    )


@provider_app.command("show")
def show_provider(
    profile: Annotated[str, typer.Argument()],
    model: Annotated[str | None, typer.Option("--model")] = None,
    provider_config: Annotated[Path | None, typer.Option("--provider-config")] = None,
) -> None:
    """Show one resolved local binding without reading its credential value."""

    try:
        binding = ProfileResolver().resolve(profile, model, provider_config)
    except ProviderError as exc:
        _command_error(exc)
    _echo_json({"schema_version": CLI_SCHEMA_VERSION, "provider": _provider_projection(binding)})


@provider_app.command("check")
def check_provider(
    profile: Annotated[str, typer.Argument()] = "deepseek",
    model: Annotated[str | None, typer.Option("--model")] = None,
    provider_config: Annotated[Path | None, typer.Option("--provider-config")] = None,
    env_file: Annotated[Path | None, typer.Option("--env-file")] = None,
    connect: Annotated[
        bool,
        typer.Option(help="Send one minimal request; disabled by default."),
    ] = False,
) -> None:
    """Validate a provider locally, optionally performing an explicit connection check."""

    try:
        binding = ProfileResolver().resolve(profile, model, provider_config, env_file)
        projection = _provider_projection(binding)
        if env_file is not None:
            try:
                CredentialResolver().resolve(binding, env_file=env_file)
            except ProviderError:
                projection["credential_configured"] = False
            else:
                projection["credential_configured"] = True
        data: dict[str, object] = {
            "schema_version": CLI_SCHEMA_VERSION,
            "provider": projection,
            "connected": False,
        }
        if connect:
            gateway, _ = _configured_provider(
                profile,
                model=model,
                config_path=provider_config,
                env_file=env_file,
            )
            response = cast(ProviderGatewayPort, gateway).complete_request(
                ProviderRequest(
                    request_id=f"provider-check-{uuid4().hex}",
                    task_id="provider-check",
                    step_index=0,
                    purpose=ProviderRequestPurpose.AGENT_STEP,
                    messages=(ModelMessage(role="user", content="Reply with OK."),),
                    max_output_tokens=1,
                )
            )
            data.update(
                {
                    "connected": True,
                    "request_id": response.request_id,
                    "finish_reason": response.finish_reason,
                    "usage": response.usage.model_dump(mode="json"),
                }
            )
    except (ProviderError, ValueError) as exc:
        _command_error(exc)
    _echo_json(data)


@session_app.command("create")
def create_session(
    context: typer.Context,
    workspace_mode: Annotated[str, typer.Option("--workspace-mode")] = "direct",
    base_revision: Annotated[str | None, typer.Option("--base-revision")] = None,
) -> None:
    """Create a persistent Session for this workspace without starting work."""

    services = _services_from_context(context)
    try:
        session = services.session.create(
            str(services.repository), workspace_mode=workspace_mode, base_revision=base_revision
        )
    except (ContractError, ValueError) as exc:
        _command_error(exc)
    _echo_json(
        _command_payload(
            services,
            data=session.model_dump(mode="json"),
            session_id=session.id,
            status=session.status.value,
        )
    )


@session_app.command("list")
def list_sessions(context: typer.Context) -> None:
    """List Sessions belonging to this workspace."""

    services = _services_from_context(context)
    sessions = services.session.list(str(services.repository))
    _echo_json(
        _command_payload(
            services,
            data={"items": [session.model_dump(mode="json") for session in sessions]},
        )
    )


@session_app.command("show")
def show_session(
    context: typer.Context,
    session_id: Annotated[str, typer.Argument()],
) -> None:
    """Show persisted conversation and current execution state."""

    services = _services_from_context(context)
    try:
        session = services.session.get(session_id)
        turns = services.session.turns(session_id)
        task = services.session.active_task(session_id)
        approvals = [] if task is None else services.approval.list(task.id)
        recoveries = [] if task is None else services.recovery.pending(task.id)
    except (ContractError, KeyError, TaskNotFoundError, ValueError) as exc:
        _command_error(exc)
    presentation = (
        None
        if task is None
        else _task_presentation(services, session_id, task, approvals, recoveries)
    )
    data: dict[str, object] = {
        "session": session.model_dump(mode="json"),
        "workspace": str(services.repository),
        "turns": [turn.model_dump(mode="json") for turn in turns],
        "active_task": presentation,
        "approvals": [_approval_projection(services, approval) for approval in approvals],
        "recovery_required": [effect.model_dump(mode="json") for effect in recoveries],
        "next_commands": _next_commands(
            services,
            session_id,
            task,
            approvals,
            recoveries,
            session_open=session.status.value == "open",
        ),
    }
    _echo_json(
        _command_payload(
            services,
            data=data,
            session_id=session.id,
            task=task,
            approvals=approvals,
            recoveries=recoveries,
            status=session.status.value,
            next_commands=cast(list[str], data["next_commands"]),
        )
    )


@session_app.command("start")
def start_session_task(
    context: typer.Context,
    session_id: Annotated[str, typer.Argument()],
    goal: Annotated[str, typer.Argument(help="Natural-language development goal.")],
    workspace_mode: Annotated[WorkspaceMode | None, typer.Option("--workspace-mode")] = None,
    base_revision: Annotated[str | None, typer.Option("--base-revision")] = None,
    prompt_cache_layout: Annotated[
        str, typer.Option(help="Prompt layout: legacy, stable or append_only.")
    ] = DEFAULT_PROMPT_CACHE_LAYOUT.value,
    append_only_optimization: Annotated[
        str,
        typer.Option(
            "--append-only-optimization",
            help="Append-only optimization: baseline_v1 or balanced_v1.",
        ),
    ] = "baseline_v1",
    allow_write: Annotated[bool, typer.Option()] = False,
    allow_execute: Annotated[bool, typer.Option()] = False,
    sandbox: Annotated[
        str, typer.Option(help="Command sandbox backend: docker or local.")
    ] = "docker",
    sandbox_image: Annotated[
        str, typer.Option(help="Docker image used by the command sandbox.")
    ] = "patchloop-sandbox:py313",
    sandbox_workspace_limit_mb: Annotated[
        int | None, typer.Option("--sandbox-workspace-limit-mb", min=64, max=16_384)
    ] = None,
    sandbox_workspace_inode_limit: Annotated[
        int, typer.Option("--sandbox-workspace-inode-limit", min=1024)
    ] = 65_536,
    provider_profile: Annotated[str | None, typer.Option("--provider")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    provider_config: Annotated[Path | None, typer.Option("--provider-config")] = None,
    env_file: Annotated[Path | None, typer.Option("--env-file")] = None,
) -> None:
    """Create the Session's active Task and run it until the next boundary."""

    services = _services_from_context(context)
    permissions = [PermissionLevel.READ.value]
    if allow_write:
        permissions.append(PermissionLevel.WRITE.value)
    if allow_execute:
        permissions.append(PermissionLevel.EXECUTE.value)
    event_writer = None if services.json_output else _HumanProviderWriter()
    try:
        _validate_sandbox_options(sandbox, sandbox_workspace_limit_mb)
        selected_session = services.session.get(session_id)
        selected_mode = workspace_mode or WorkspaceMode(selected_session.workspace_mode)
        if workspace_mode is not None or selected_mode == WorkspaceMode.WORKTREE:
            prepare_session_workspace(
                services.store,
                session_id,
                selected_mode,
                base_revision or selected_session.workspace_base_revision,
            )
        cache_layout = PromptCacheLayout(prompt_cache_layout)
        optimization = _parse_append_only_optimization(
            append_only_optimization,
            cache_layout,
        )
        active_task = services.session.active_task(session_id)
        if active_task is not None:
            raise LeaseConflict(session_id, active_task.id)
        load_policy_configuration(services.repository)
        provider, binding = _selected_provider(
            provider_profile,
            model=model,
            config_path=provider_config,
            env_file=env_file,
        )
        task = services.session.start_task(
            session_id,
            goal,
            execution=TaskExecutionConfig(
                prompt_cache_layout=cache_layout,
                append_only_optimization=optimization,
                allowed_permissions=permissions,
                non_interactive=True,
                sandbox_backend=sandbox,
                sandbox_image=sandbox_image,
                sandbox_workspace_limit_mb=sandbox_workspace_limit_mb,
                sandbox_workspace_inode_limit=sandbox_workspace_inode_limit,
                provider=binding,
            ),
        )
    except ApprovalPending as exc:
        _echo_json(
            {
                "schema_version": CLI_SCHEMA_VERSION,
                "status": "approval_required",
                "approval_id": exc.approval.id,
            }
        )
        raise typer.Exit(code=int(CliExitCode.WAITING_FOR_APPROVAL)) from None
    except (
        ContractError,
        KeyError,
        OSError,
        ProviderError,
        TaskNotFoundError,
        ValueError,
    ) as exc:
        _command_error(exc)
    try:
        result = _session_runtime_service(
            services,
            task,
            provider=provider,
            provider_event_observer=event_writer,
        ).resume(session_id)
    except KeyboardInterrupt:
        _pause_interrupted_session(services, session_id)
    except (
        ContractError,
        KeyError,
        OSError,
        ProviderError,
        RuntimeError,
        TaskNotFoundError,
        ValueError,
    ) as exc:
        _command_error(exc)
    if event_writer is not None:
        event_writer.flush()
        _echo_human_task(result)
        if (exit_code := _task_exit_code(result)) is not CliExitCode.SUCCESS:
            raise typer.Exit(code=int(exit_code))
    else:
        _echo_task_result(services, session_id, result, exit_for_state=True)


@session_app.command("send")
def send_session_message(
    context: typer.Context,
    session_id: Annotated[str, typer.Argument()],
    message: Annotated[str, typer.Argument()],
    client_submission_id: Optional[str] = typer.Option(None),  # noqa: UP045
) -> None:
    """Persist a message for the active Task."""

    services = _services_from_context(context)
    try:
        if services.session.active_task(session_id) is None:
            raise ValueError(f"session {session_id} has no active task")
        turn = services.session.append_message(
            session_id,
            message,
            client_submission_id=client_submission_id,
        )
    except (ContractError, KeyError, TaskNotFoundError, ValueError) as exc:
        _command_error(exc)
    task = services.session.active_task(session_id)
    _echo_json(
        _command_payload(
            services,
            data=turn.model_dump(mode="json"),
            session_id=session_id,
            task=task,
        )
    )


@session_app.command("pause")
def pause_session(context: typer.Context, session_id: Annotated[str, typer.Argument()]) -> None:
    """Persist a pause request for the active Task."""

    services = _services_from_context(context)
    try:
        request = services.session.request_pause(session_id)
    except (ContractError, KeyError, TaskNotFoundError, ValueError) as exc:
        _command_error(exc)
    task = services.session.active_task(session_id)
    _echo_json(
        _command_payload(
            services,
            data=request.model_dump(mode="json"),
            session_id=session_id,
            task=task,
            status="pause_requested",
        )
    )


@session_app.command("cancel")
def cancel_session(context: typer.Context, session_id: Annotated[str, typer.Argument()]) -> None:
    """Persist a final cancellation request for the active Task."""

    services = _services_from_context(context)
    try:
        request = services.session.request_cancel(session_id)
    except (ContractError, KeyError, TaskNotFoundError, ValueError) as exc:
        _command_error(exc)
    task = services.session.active_task(session_id)
    _echo_json(
        _command_payload(
            services,
            data=request.model_dump(mode="json"),
            session_id=session_id,
            task=task,
            status="cancel_requested",
        )
    )


@session_app.command("resume")
def resume_session(
    context: typer.Context,
    session_id: Annotated[str, typer.Argument()],
    legacy_provider: Annotated[str | None, typer.Option("--legacy-provider")] = None,
    provider_config: Annotated[Path | None, typer.Option("--provider-config")] = None,
    env_file: Annotated[Path | None, typer.Option("--env-file")] = None,
) -> None:
    """Resume the active Task without changing its persisted authorization."""

    services = _services_from_context(context)
    try:
        task = services.session.active_task(session_id)
        if task is None:
            raise ValueError(f"session {session_id} has no active task")
        load_policy_configuration(services.repository, session_id=task.session_id)
        provider = None
        if legacy_provider is not None:
            if task.execution.provider is not None:
                raise CliUsageError("provider override is forbidden for a bound task")
            provider, binding = _configured_provider(
                legacy_provider,
                config_path=provider_config,
                env_file=env_file,
            )
            task = task.model_copy(
                update={"execution": task.execution.model_copy(update={"provider": binding})}
            )
    except (ContractError, KeyError, OSError, ProviderError, TaskNotFoundError, ValueError) as exc:
        _command_error(exc)
    event_writer = None if services.json_output else _HumanProviderWriter()
    try:
        runtime_service = (
            _session_runtime_service(services, task)
            if provider is None and event_writer is None
            else _session_runtime_service(
                services,
                task,
                provider=provider,
                provider_event_observer=event_writer,
            )
        )
        result = runtime_service.resume(session_id)
    except KeyboardInterrupt:
        _pause_interrupted_session(services, session_id)
    except (
        ContractError,
        KeyError,
        OSError,
        ProviderError,
        RuntimeError,
        TaskNotFoundError,
        ValueError,
    ) as exc:
        _command_error(exc)
    if event_writer is not None:
        event_writer.flush()
        _echo_human_task(result)
        if (exit_code := _task_exit_code(result)) is not CliExitCode.SUCCESS:
            raise typer.Exit(code=int(exit_code))
    else:
        _echo_task_result(services, session_id, result, exit_for_state=True)


@session_app.command("close")
def close_session(context: typer.Context, session_id: Annotated[str, typer.Argument()]) -> None:
    """Close a Session which has no active Task."""

    services = _services_from_context(context)
    try:
        session = services.session.close(session_id)
    except (ContractError, KeyError, TaskNotFoundError, ValueError) as exc:
        _command_error(exc)
    _echo_json(
        _command_payload(
            services,
            data=session.model_dump(mode="json"),
            session_id=session.id,
            status=session.status.value,
        )
    )


@session_app.command("recover")
def show_session_recovery(
    context: typer.Context,
    session_id: Annotated[str, typer.Argument()],
    effect_id: Optional[str] = typer.Option(None),  # noqa: UP045
    abandon: Annotated[bool, typer.Option()] = False,
    retry: Annotated[bool, typer.Option()] = False,
    acknowledge_duplicate_risk: Annotated[
        bool,
        typer.Option(
            "--acknowledge-duplicate-risk",
            help="Acknowledge that retry may repeat an external side effect.",
        ),
    ] = False,
    confirm_success: Annotated[bool, typer.Option()] = False,
    confirm_failure: Annotated[bool, typer.Option()] = False,
    result: Annotated[str, typer.Option()] = "",
    evidence: Annotated[str, typer.Option(help="JSON object with operator evidence.")] = "{}",
    source: Annotated[str, typer.Option()] = "cli",
) -> None:
    """Inspect or explicitly resolve an Effect whose result is unknown."""

    services = _services_from_context(context)
    try:
        task = services.session.active_task(session_id)
        effects = [] if task is None else services.recovery.pending(task.id)
        actions = sum((abandon, retry, confirm_success, confirm_failure))
        if actions == 0:
            repository = json.dumps(str(services.repository), ensure_ascii=False)
            command_prefix = f"patchloop session --repo {repository} recover {session_id}"
            items = [
                {
                    **effect.model_dump(mode="json"),
                    "result_state": "unknown",
                    "retry_may_duplicate_external_side_effect": True,
                    "retry_requires_duplicate_risk_acknowledgement": True,
                    "warning": (
                        "The original action result is unknown. Retrying may repeat an "
                        "external side effect."
                    ),
                    "next_commands": [
                        f"{command_prefix} --effect-id {effect.id} --confirm-success "
                        '--result "<observed result>" --evidence \'{"verified_by":"<name>"}\'',
                        f"{command_prefix} --effect-id {effect.id} --retry "
                        "--acknowledge-duplicate-risk "
                        "--evidence '{\"duplicate_risk_acknowledged\":true}'",
                        f"{command_prefix} --effect-id {effect.id} --abandon "
                        '--evidence \'{"reason":"<reason>"}\'',
                    ],
                }
                for effect in effects
            ]
            _echo_json(
                _command_payload(
                    services,
                    data={"items": items},
                    session_id=session_id,
                    task=task,
                    recoveries=effects,
                    next_commands=[
                        command
                        for item in items
                        for command in cast(list[str], item["next_commands"])
                    ],
                )
            )
            return
        if actions != 1 or effect_id is None or task is None:
            raise ValueError(
                "choose exactly one recovery action and provide --effect-id for an active task"
            )
        evidence_value = json.loads(evidence)
        if not isinstance(evidence_value, dict) or not evidence_value:
            raise ValueError("recovery action requires a non-empty JSON evidence object")
        if abandon:
            resolution = services.recovery.abandon_pending(
                task_id=task.id,
                unknown_effect_id=effect_id,
                evidence=evidence_value,
                decision_source=source,
            )
        elif retry:
            if not acknowledge_duplicate_risk:
                if services.json_output:
                    raise ValueError(
                        "--retry requires --acknowledge-duplicate-risk because the "
                        "original action result is unknown and retry may duplicate it"
                    )
                acknowledge_duplicate_risk = typer.confirm(
                    "The original action result is unknown and retry may duplicate an "
                    "external side effect. Create a new retry Effect?",
                    default=False,
                )
            resolution = services.recovery.retry_pending(
                task_id=task.id,
                unknown_effect_id=effect_id,
                duplicate_risk_acknowledged=acknowledge_duplicate_risk,
                evidence=evidence_value,
                decision_source=source,
                policy_version=_tool_policy(task).version,
                config_version=services.session.get(session_id).config_version,
            )
        else:
            resolution = services.recovery.confirm_persisted_result(
                task_id=task.id,
                unknown_effect_id=effect_id,
                success=confirm_success,
                output=result,
                evidence=evidence_value,
                decision_source=source,
            )
    except (ContractError, json.JSONDecodeError, KeyError, TaskNotFoundError, ValueError) as exc:
        _command_error(exc)
    resolution_data: dict[str, object] = {
        "task": resolution.task.model_dump(mode="json"),
        "disposition": resolution.disposition.model_dump(mode="json"),
        "retry_effect": (
            None
            if resolution.retry_effect is None
            else resolution.retry_effect.model_dump(mode="json")
        ),
        "retry_approval": (
            None
            if resolution.retry_approval is None
            else resolution.retry_approval.model_dump(mode="json")
        ),
    }
    approvals = services.approval.list(resolution.task.id)
    recoveries = services.recovery.pending(resolution.task.id)
    presentation = _task_presentation(
        services,
        session_id,
        resolution.task,
        approvals,
        recoveries,
    )
    presentation["recovery_resolution"] = resolution_data
    _echo_json(
        _command_payload(
            services,
            data=presentation,
            session_id=session_id,
            task=resolution.task,
            approvals=approvals,
            recoveries=recoveries,
            next_commands=cast(list[str], presentation["next_commands"]),
        )
    )
    if (exit_code := _task_exit_code(resolution.task)) is not CliExitCode.SUCCESS:
        raise typer.Exit(code=int(exit_code))


@session_app.command("enter")
def enter_session(
    context: typer.Context,
    session_id: Annotated[str, typer.Argument()],
) -> None:
    """Enter a lightweight persistent-message loop; use /exit to leave it."""

    services = _services_from_context(context)
    try:
        services.session.get(session_id)
    except (ContractError, KeyError, ValueError) as exc:
        _command_error(exc)
    if not services.json_output:
        typer.echo(f"Entered session {session_id}. Use /exit to leave without cancelling.")
    while True:
        try:
            message = input() if services.json_output else typer.prompt("message")
        except (KeyboardInterrupt, typer.Abort):
            _pause_interrupted_session(services, session_id)
        if message.strip() in {"/exit", "/quit"}:
            exit_payload = _command_payload(
                services,
                data={"exited": True, "control_requested": False},
                session_id=session_id,
                task=services.session.active_task(session_id),
                status="exited",
                next_commands=[],
            )
            (_echo_json_line if services.json_output else _echo_json)(exit_payload)
            return
        if not message.strip():
            continue
        try:
            if services.session.active_task(session_id) is None:
                raise ValueError(f"session {session_id} has no active task")
            turn = services.session.append_message(session_id, message)
        except (ContractError, KeyError, TaskNotFoundError, ValueError) as exc:
            _command_error(exc)
        turn_payload = _command_payload(
            services,
            data=turn.model_dump(mode="json"),
            session_id=session_id,
            task=services.session.active_task(session_id),
        )
        (_echo_json_line if services.json_output else _echo_json)(turn_payload)


@approval_app.command("list")
def list_approvals(
    context: typer.Context,
    task_id: Annotated[str, typer.Argument()],
) -> None:
    """List persisted approval requests for a Task."""

    services = _services_from_context(context)
    try:
        approvals = services.approval.list(task_id)
        task = services.approval.task(task_id)
    except (ContractError, KeyError, TaskNotFoundError, ValueError) as exc:
        _command_error(exc)
    repository = json.dumps(str(services.repository), ensure_ascii=False)
    items = [
        {
            **_approval_projection(services, approval),
            "next_commands": (
                [
                    f"patchloop approval --repo {repository} decide {approval.id} --approve",
                    f"patchloop approval --repo {repository} decide {approval.id} --deny",
                ]
                if approval.status.value == "pending"
                else []
            ),
        }
        for approval in approvals
    ]
    if not services.json_output:
        for item in items:
            typer.echo(
                f"{item['id']} {item['status']} scope={item['scope_kind']} "
                f"expires={item['expires_at']} policy={item['policy_version']}\n"
                f"  {item['resources']}\n  rules={item['matched_rule_ids']}"
            )
            for command in cast(list[str], item["next_commands"]):
                typer.echo(f"  {command}")
        return
    _echo_json(
        _command_payload(
            services,
            data={"items": items},
            session_id=task.session_id,
            task=task,
            approvals=approvals,
            next_commands=[
                command for item in items for command in cast(list[str], item["next_commands"])
            ],
        )
    )


@approval_app.command("decide")
def decide_approval(
    context: typer.Context,
    approval_id: Annotated[str, typer.Argument()],
    approved: Optional[bool] = typer.Option(None, "--approve/--deny"),  # noqa: UP045
    source: Annotated[str, typer.Option()] = "cli",
    scope: Annotated[ApprovalScopeKind, typer.Option()] = ApprovalScopeKind.ONCE,
    expires: Annotated[
        str | None, typer.Option(help="ISO-8601 expiry with timezone, within 24h.")
    ] = None,
    reason: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Approve once or deny an exact persisted Effect request."""

    services = _services_from_context(context)
    try:
        if approved is None:
            raise ValueError("choose exactly one of --approve or --deny")
        resolution = services.approval.decide_current(
            approval_id,
            approved=approved,
            source=source,
            scope_kind=scope,
            expires_at=None if expires is None else datetime.fromisoformat(expires),
            reason=reason,
        )
    except (ContractError, KeyError, TaskNotFoundError, ValueError) as exc:
        _command_error(exc)
    approval, effect, task = resolution
    if not services.json_output:
        typer.echo(
            f"{approval.id} {approval.status.value} scope={approval.scope_kind.value} "
            f"grant={approval.grant_id} expires={approval.expires_at}\n"
            f"{approval.resource_summary}"
        )
        return
    _echo_json(
        _command_payload(
            services,
            data={
                "approval": _approval_projection(services, approval),
                "grant": None
                if resolution.grant is None
                else resolution.grant.model_dump(mode="json"),
                "effect": effect.model_dump(mode="json"),
                "task": task.model_dump(mode="json"),
            },
            session_id=task.session_id,
            task=task,
        )
    )


@grant_app.command("list")
def list_approval_grants(
    context: typer.Context, scope_id: Annotated[str, typer.Argument()]
) -> None:
    services = _services_from_context(context)
    try:
        grants = services.approval.list_grants(scope_id)
    except (ContractError, KeyError, ValueError) as exc:
        _command_error(exc)
    if not services.json_output:
        for grant in grants:
            typer.echo(
                f"{grant.id} {grant.status} scope={grant.scope_kind.value} "
                f"workspace={grant.workspace_ref} session={grant.session_id} "
                f"expires={grant.expires_at} resources="
                + ", ".join(resource.value for resource in grant.resources)
            )
        return
    _echo_json(
        _command_payload(
            services,
            data={
                "items": [
                    {
                        **grant.model_dump(mode="json"),
                        "next_commands": [
                            f"patchloop approval --repo {json.dumps(str(services.repository))} "
                            f"grant revoke {grant.id}"
                        ]
                        if grant.status == "active"
                        else [],
                    }
                    for grant in grants
                ]
            },
        )
    )


@grant_app.command("revoke")
def revoke_approval_grant(
    context: typer.Context,
    grant_id: Annotated[str, typer.Argument()],
    reason: Annotated[str | None, typer.Option()] = None,
    source: Annotated[str, typer.Option()] = "cli",
) -> None:
    services = _services_from_context(context)
    try:
        grant = services.approval.revoke_grant(grant_id, source=source, reason=reason)
    except (ContractError, KeyError, ValueError) as exc:
        _command_error(exc)
    if services.json_output:
        _echo_json(_command_payload(services, data={"grant": grant.model_dump(mode="json")}))
    else:
        typer.echo(f"Revoked {grant.id}; subsequent effects require authorization.")


@policy_app.command("list")
def list_policy(context: typer.Context) -> None:
    services = _services_from_context(context)
    try:
        configuration = load_policy_configuration(services.repository)
        rules = [*configuration.rules, *services.store.list_policy_rules(str(services.repository))]
    except (ContractError, KeyError, ValueError) as exc:
        _command_error(exc)
    if services.json_output:
        _echo_json(
            _command_payload(
                services, data={"items": [rule.model_dump(mode="json") for rule in rules]}
            )
        )
    else:
        for rule in rules:
            typer.echo(
                f"{rule.id} {rule.source} {rule.effect.value} {rule.action.value} "
                f"{rule.pattern} policy={rule.policy_version}"
            )


@policy_app.command("explain")
def explain_policy(context: typer.Context, identifier: Annotated[str, typer.Argument()]) -> None:
    services = _services_from_context(context)
    try:
        try:
            effects = [services.store.get_effect(identifier)]
        except KeyError:
            services.store.get_task(identifier)
            effects = services.store.list_effects(identifier)
    except (ContractError, KeyError, TaskNotFoundError, ValueError) as exc:
        _command_error(exc)
    items = [
        {
            "effect_id": effect.id,
            "status": effect.status.value,
            "descriptor": None
            if effect.action_descriptor is None
            else effect.action_descriptor.model_dump(mode="json"),
            "evaluation": None
            if effect.policy_evaluation is None
            else effect.policy_evaluation.model_dump(mode="json"),
            "matched_grant_id": effect.consumed_grant_id,
            "policy_result": effect.policy_result,
        }
        for effect in effects
    ]
    if services.json_output:
        _echo_json(_command_payload(services, data={"items": items}))
    else:
        for item in items:
            typer.echo(json.dumps(item, ensure_ascii=False, indent=2))


def _all_tools() -> list[Tool]:
    return [
        ListFilesTool(),
        ReadFileTool(),
        SearchTextTool(),
        SearchCodeTool(),
        UpdatePlanTool(),
        CreateFileTool(),
        ApplyPatchTool(),
        ReplaceTextTool(),
        WriteFileTool(),
        RunCommandTool(),
        RunTestsTool(),
        GetDiffTool(),
    ]


def _create_sandbox(
    backend: str,
    image: str,
    workspace_limit_mb: int | None = None,
    workspace_inode_limit: int = 65_536,
) -> DockerSandbox | LocalProcessSandbox:
    return create_command_sandbox(
        TaskExecutionConfig(
            sandbox_backend=backend,
            sandbox_image=image,
            sandbox_workspace_limit_mb=workspace_limit_mb,
            sandbox_workspace_inode_limit=workspace_inode_limit,
        )
    )


def _validate_sandbox_options(backend: str, workspace_limit_mb: int | None) -> None:
    if backend == "local" and workspace_limit_mb is not None:
        raise CliUsageError("workspace limits require the Docker sandbox")


@task_app.command("create")
def create_task(
    goal: Annotated[str, typer.Argument(help="Natural-language development goal.")],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    task = Task(goal=goal, repository=str(repo.resolve()))
    _sqlite_store(repo).save_task(task)
    typer.echo(task.model_dump_json(indent=2))


@task_app.command("show")
def show_task(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    try:
        task = _sqlite_store(repo).get_task(task_id)
    except TaskNotFoundError:
        typer.echo(f"task not found: {task_id}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(task.model_dump_json(indent=2))


@app.command("tools")
def list_tools(
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    gateway = ToolGateway(
        ToolContext(repo),
        _all_tools(),
    )
    typer.echo(json.dumps([item.model_dump() for item in gateway.specifications()], indent=2))


def _index_path(repository: Path) -> Path:
    return _state_dir(repository) / "repository-index.json"


def _load_or_build_index(repository: Path) -> tuple[RepositorySnapshot, bool]:
    indexer = RepositoryIndexer(repository)
    path = _index_path(repository)
    if path.is_file():
        try:
            snapshot = RepositorySnapshot.load(path)
            if indexer.is_current(snapshot):
                return snapshot, False
        except (OSError, ValueError):
            pass
    snapshot = indexer.build()
    snapshot.save(path)
    return snapshot, True


@app.command("index")
def index_repository(
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    snapshot = RepositoryIndexer(repo).build()
    path = snapshot.save(_index_path(repo))
    typer.echo(
        json.dumps(
            {
                "path": str(path),
                "files": len(snapshot.files),
                "symbols": snapshot.symbol_count,
                "references": snapshot.reference_count,
                "test_mappings": len(snapshot.test_mappings),
            },
            indent=2,
        )
    )


@app.command("search")
def search_repository(
    query: Annotated[str, typer.Argument(help="Natural-language code location query.")],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    limit: Annotated[int, typer.Option(min=1, max=50)] = 10,
) -> None:
    snapshot, rebuilt = _load_or_build_index(repo)
    hits = RepositorySearch(repo, snapshot).search(query, limit=limit)
    typer.echo(
        json.dumps(
            {
                "index_rebuilt": rebuilt,
                "results": [hit.model_dump(mode="json") for hit in hits],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("benchmark-search")
def benchmark_repository_search(
    tasks: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, resolve_path=True),
    ] = Path("benchmarks/retrieval_tasks.json"),
    root: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    output: Annotated[str, typer.Option(help="Optional JSON report path.")] = "",
) -> None:
    report = evaluate_retrieval(load_retrieval_tasks(tasks), root)
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(report.model_dump_json(indent=2))


@app.command("benchmark")
def benchmark_evaluation(
    manifest: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, resolve_path=True),
    ] = Path("benchmarks/evaluation_manifest.json"),
    root: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    output: Annotated[
        Path,
        typer.Option(dir_okay=False, resolve_path=True),
    ] = Path("benchmarks/results/week08_evaluation.json"),
    variant: Annotated[
        str,
        typer.Option(help="single_shot, no_plan, text_only, patchloop, or all."),
    ] = "all",
    jobs: Annotated[int, typer.Option(min=1, max=64)] = 4,
    retries: Annotated[int, typer.Option(min=0, max=10)] = 1,
) -> None:
    try:
        task_manifest = load_evaluation_manifest(manifest)
        variants: list[EvaluationVariant] = (
            list(EvaluationVariant) if variant == "all" else [EvaluationVariant(variant)]
        )
        report = EvaluationRunner(root, jobs=jobs, retries=retries).run_suite(
            task_manifest,
            [RetrievalBaseline(item) for item in variants],
        )
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(report.model_dump_json(indent=2))


@app.command("benchmark-code")
def benchmark_code_tasks(
    manifest: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, resolve_path=True),
    ] = Path("benchmarks/coding_tasks.json"),
    root: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    work_root: Annotated[
        Path,
        typer.Option(file_okay=False, resolve_path=True),
    ] = Path(".patchloop/code-benchmark"),
    output: Annotated[
        Path,
        typer.Option(dir_okay=False, resolve_path=True),
    ] = Path("benchmarks/results/code_benchmark_latest.json"),
    repeats: Annotated[int, typer.Option(min=1, max=20)] = 1,
    task: Annotated[
        str,
        typer.Option("--task", help="Comma-separated task ids to run."),
    ] = "",
    sandbox: Annotated[str, typer.Option(help="docker or local")] = "docker",
    sandbox_image: Annotated[
        str,
        typer.Option(help="Container image used by the Docker sandbox."),
    ] = "python:3.12-slim",
) -> None:
    try:
        task_manifest = load_coding_manifest(manifest)
        if task:
            selected = {item.strip() for item in task.split(",") if item.strip()}
            available = {item.id for item in task_manifest.tasks}
            unknown = sorted(selected - available)
            if unknown:
                raise ValueError(f"unknown coding task ids: {', '.join(unknown)}")
            task_manifest = task_manifest.model_copy(
                update={"tasks": [item for item in task_manifest.tasks if item.id in selected]}
            )
        report = CodingBenchmarkRunner(
            root,
            work_root,
            lambda: DeepSeekProvider.from_env(root / ".env"),
            lambda: _create_sandbox(sandbox, sandbox_image),
            repeats=repeats,
        ).run(task_manifest)
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(report.model_dump_json(indent=2))


@app.command("benchmark-memory")
def benchmark_memory(
    manifest: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, resolve_path=True),
    ] = Path("benchmarks/memory_tasks.json"),
    root: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    output: Annotated[
        Path,
        typer.Option(dir_okay=False, resolve_path=True),
    ] = Path("benchmarks/results/lcm00_memory_baseline.json"),
    variant: Annotated[
        str,
        typer.Option(
            help=(
                "recent_only, task_memory_v1, hierarchical_no_semantic, "
                "hierarchical_no_episodic, hierarchical_no_compression, or hierarchical_memory."
            )
        ),
    ] = "task_memory_v1",
    mode: Annotated[
        str,
        typer.Option(help="deterministic or model."),
    ] = "deterministic",
    repeats: Annotated[int, typer.Option(min=1, max=20)] = 1,
    task: Annotated[
        str,
        typer.Option("--task", help="Comma-separated memory task ids to run."),
    ] = "",
) -> None:
    try:
        task_manifest = load_memory_manifest(manifest)
        if task:
            selected = {item.strip() for item in task.split(",") if item.strip()}
            available = {item.id for item in task_manifest.tasks}
            unknown = sorted(selected - available)
            if unknown:
                raise ValueError(f"unknown memory task ids: {', '.join(unknown)}")
            task_manifest = task_manifest.model_copy(
                update={"tasks": [item for item in task_manifest.tasks if item.id in selected]}
            )
        benchmark_mode = MemoryBenchmarkMode(mode)
        report = MemoryBenchmarkRunner(
            (
                (lambda: DeepSeekProvider.from_env(root / ".env"))
                if benchmark_mode is MemoryBenchmarkMode.MODEL
                else None
            ),
            repeats=repeats,
        ).run(
            task_manifest,
            variant=MemoryBenchmarkVariant(variant),
            mode=benchmark_mode,
        )
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(report.model_dump_json(indent=2))


def _git_revision(repository: Path) -> str:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    if not revision:
        raise ValueError("git revision is unavailable")
    return revision


@app.command("benchmark-cache")
def benchmark_cache(
    mode: Annotated[str, typer.Option(help="deterministic or provider.")] = "deterministic",
    suite: Annotated[
        str,
        typer.Option(help="cache-matrix, prefix-runtime or append-only-overhead."),
    ] = "cache-matrix",
    trace_manifest: Annotated[
        str, typer.Option(help="JSON manifest of paired Provider trace runs.")
    ] = "",
    repository: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path("."),
    trace: Annotated[str, typer.Option(help="JSONL provider trace when mode=provider.")] = "",
    output: Annotated[
        Path,
        typer.Option(dir_okay=False, resolve_path=True),
    ] = Path("benchmarks/results/pco06_cache_matrix.json"),
    repeats: Annotated[int, typer.Option(min=3, max=20)] = 3,
) -> None:
    try:
        if suite not in {"cache-matrix", "prefix-runtime", "append-only-overhead"}:
            raise ValueError("suite must be cache-matrix, prefix-runtime or append-only-overhead")
        if suite == "append-only-overhead" and repeats != 3:
            raise ValueError("append-only-overhead requires exactly three repeats")
        if mode == "deterministic":
            runner = CacheBenchmarkRunner(repeats=repeats)
            if suite == "prefix-runtime":
                report = runner.run_prefix_suite(repository)
            elif suite == "append-only-overhead":
                report = runner.run_append_only_overhead_suite(repository)
                source_root = Path(__file__).resolve().parents[2]
                report = report.model_copy(
                    update={
                        "revision": _git_revision(source_root),
                        "source_fingerprint": aop_source_fingerprint(source_root),
                    }
                )
            else:
                report = runner.run()
        elif mode == "provider":
            if suite == "append-only-overhead":
                raise ValueError("append-only-overhead is an offline deterministic suite")
            if suite == "prefix-runtime" and not trace_manifest:
                raise ValueError(
                    "PPS provider evaluation requires --trace-manifest with paired runs"
                )
            if trace_manifest:
                report = _provider_prefix_manifest(Path(trace_manifest))
            elif not trace:
                raise ValueError("--trace is required when mode=provider")
            else:
                report = _provider_cache_report(Path(trace))
        else:
            raise ValueError("mode must be deterministic or provider")
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(report.human_summary())
    typer.echo(f"machine_report={output}")


def _provider_cache_report(trace: Path) -> CacheEvaluationReport:
    run = RealProviderCacheCollector.run_from_events(
        EventLogger(trace).read(),
        variant=CacheEvaluationVariant.FULL_OPTIMIZATION,
    )
    summary = summarize_cache_run(run)
    return CacheEvaluationReport(
        repeats=1,
        variants=(run.variant,),
        fixture_fingerprint="0" * 64,
        runs=[run],
        summaries=[summary],
    )


def _provider_prefix_manifest(path: Path) -> CacheEvaluationReport:
    """Import explicitly identified runs; never infer experiment pairs from event positions."""
    from patchloop.evaluation.cache import CacheRunReport

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        raise ValueError("trace manifest must contain a runs array")
    runs: list[CacheRunReport] = []
    for entry in data["runs"]:
        if not isinstance(entry, dict) or not all(
            k in entry
            for k in (
                "trace",
                "variant",
                "repeat",
                "batch_id",
                "task_case",
                "pair_id",
            )
        ):
            raise ValueError(
                "each trace run requires trace/variant/repeat/batch_id/task_case/pair_id"
            )
        variant = CacheEvaluationVariant(entry["variant"])
        if variant not in {
            CacheEvaluationVariant.CURRENT_LAYOUT,
            CacheEvaluationVariant.APPEND_ONLY,
        }:
            raise ValueError("PPS trace variants must be current_layout or append_only")
        runs.append(
            RealProviderCacheCollector.run_from_events(
                EventLogger(path.parent / entry["trace"]).read(),
                variant=variant,
                repeat=entry["repeat"],
                batch_id=entry["batch_id"],
                task_case=entry["task_case"],
                pair_id=entry["pair_id"],
                task_id=entry.get("task_id"),
            )
        )
    return CacheEvaluationReport(
        schema_version="pps.v1",
        suite_id="pps-provider-pairs",
        repeats=max((r.repeat for r in runs), default=1),
        variants=tuple(dict.fromkeys(r.variant for r in runs)),
        fixture_fingerprint="0" * 64,
        runs=runs,
        summaries=[summarize_cache_run(run) for run in runs],
    )


@app.command("validate-cache-gates")
def validate_cache_gates(
    profile: Annotated[str, typer.Option(help="pco, pps or aop acceptance profile.")] = "pco",
    baseline_report: Annotated[
        str, typer.Option(help="Same-batch legacy baseline report for PPS.")
    ] = "",
    enable_append_only: Annotated[
        bool, typer.Option(help="Request PPS rollout after all gates pass.")
    ] = False,
    report: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, resolve_path=True),
    ] = Path("benchmarks/results/pco06_cache_matrix.json"),
    local_report: Annotated[
        str,
        typer.Option(help="Optional deterministic report for compression/input gates."),
    ] = "",
    quality: Annotated[
        str,
        typer.Option(help="JSON file containing PCO-07 memory/security evidence."),
    ] = "",
    output: Annotated[
        Path,
        typer.Option(dir_okay=False, resolve_path=True),
    ] = Path("benchmarks/results/pco07_acceptance.json"),
    enable_stable: Annotated[
        bool,
        typer.Option(help="Evaluate the stable layout as the selected gray candidate."),
    ] = False,
    allow_simulated: Annotated[
        bool,
        typer.Option(help="Allow deterministic cache data for local dry-run gating."),
    ] = False,
) -> None:
    try:
        cache_report = CacheEvaluationReport.model_validate_json(report.read_text(encoding="utf-8"))
        if profile == "aop":
            repository_root = Path(__file__).resolve().parents[2]
            report_root = output.parent.resolve()
            report_path = report.resolve()
            if report_path == output.resolve():
                raise ValueError("AOP readiness output must not overwrite its evidence report")
            try:
                relative_report = report_path.relative_to(report_root).as_posix()
            except ValueError as exc:
                raise ValueError(
                    "AOP evidence report must be inside the readiness report directory"
                ) from exc
            aop_acceptance = CacheAcceptanceEvaluator().evaluate_optimization(
                cache_report,
                evidence=AopOptimizationEvidence(
                    revision=_git_revision(repository_root),
                    source_fingerprint=aop_source_fingerprint(repository_root),
                    fixture_fingerprint=append_only_overhead_fixture_fingerprint(
                        cache_report.repeats
                    ),
                    evidence_files=[
                        AopEvidenceFile(
                            path=relative_report,
                            sha256=hashlib.sha256(report_path.read_bytes()).hexdigest(),
                        )
                    ],
                ),
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(aop_acceptance.model_dump_json(indent=2), encoding="utf-8")
            typer.echo(aop_acceptance.human_summary())
            typer.echo(f"machine_report={output}")
            if not aop_acceptance.ready_for_bounded_validation:
                raise typer.Exit(code=1)
            return
        if profile not in {"pco", "pps"}:
            raise ValueError("cache gate profile must be pco, pps or aop")
        deterministic_report = (
            CacheEvaluationReport.model_validate_json(
                Path(local_report).read_text(encoding="utf-8")
            )
            if local_report
            else None
        )
        quality_evidence = (
            MemoryQualityEvidence.model_validate_json(Path(quality).read_text(encoding="utf-8"))
            if quality
            else None
        )
        acceptance = CacheAcceptanceEvaluator(
            require_provider_reported=not allow_simulated
        ).evaluate(
            cache_report,
            quality=quality_evidence,
            local_report=deterministic_report,
            profile=profile,
            baseline_report=(
                CacheEvaluationReport.model_validate_json(
                    Path(baseline_report).read_text(encoding="utf-8")
                )
                if baseline_report
                else None
            ),
            rollout=CacheRolloutPolicy(
                enabled=enable_append_only if profile == "pps" else enable_stable,
                candidate_layout=PromptCacheLayout.APPEND_ONLY
                if profile == "pps"
                else PromptCacheLayout.STABLE,
            ),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(acceptance.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(acceptance.human_summary())
    typer.echo(f"machine_report={output}")
    if not acceptance.passed:
        raise typer.Exit(code=1)


@app.command("experiment-memory")
def run_memory_ablation(
    manifest: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, resolve_path=True),
    ] = Path("benchmarks/memory_tasks.json"),
    root: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    output: Annotated[
        Path,
        typer.Option(dir_okay=False, resolve_path=True),
    ] = Path("benchmarks/results/lcm10_memory_ablation.json"),
    mode: Annotated[str, typer.Option(help="deterministic or model.")] = "deterministic",
    repeats: Annotated[int, typer.Option(min=1, max=20)] = 3,
    variant: Annotated[
        str,
        typer.Option(help="Comma-separated memory variants; omit to run all six LCM-10 variants."),
    ] = "",
) -> None:
    try:
        task_manifest = load_memory_manifest(manifest)
        benchmark_mode = MemoryBenchmarkMode(mode)
        selected = None
        if variant:
            selected = [MemoryBenchmarkVariant(item.strip()) for item in variant.split(",")]
        runner = MemoryAblationRunner(
            (
                (lambda: DeepSeekProvider.from_env(root / ".env"))
                if benchmark_mode is MemoryBenchmarkMode.MODEL
                else None
            ),
            repeats=repeats,
        )
        report = runner.run(task_manifest, mode=benchmark_mode, variants=selected)
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(report.model_dump_json(indent=2))


@app.command("experiment")
def run_evaluation_experiments(
    manifest: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, resolve_path=True),
    ] = Path("benchmarks/evaluation_manifest.json"),
    root: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    output: Annotated[
        Path,
        typer.Option(dir_okay=False, resolve_path=True),
    ] = Path("benchmarks/results/week09_experiments.json"),
    repeats: Annotated[int, typer.Option(min=1, max=20)] = 5,
    jobs: Annotated[int, typer.Option(min=1, max=64)] = 4,
    retries: Annotated[int, typer.Option(min=0, max=10)] = 0,
) -> None:
    try:
        task_manifest = load_evaluation_manifest(manifest)
        report = ExperimentRunner(
            root,
            jobs=jobs,
            retries=retries,
            repeats=repeats,
        ).run(task_manifest)
    except (OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(report.model_dump_json(indent=2))


@app.command("trace")
def show_trace(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    logger = EventLogger(_state_dir(repo) / "traces" / f"{task_id}.jsonl")
    for event in logger.read():
        typer.echo(event.model_dump_json())


@app.command("metrics")
def show_metrics(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    events = EventLogger(_state_dir(repo) / "traces" / f"{task_id}.jsonl").read()
    if not events:
        typer.echo(f"trace not found: {task_id}", err=True)
        raise typer.Exit(code=1)
    typer.echo(TaskMetrics.from_events(task_id, events).model_dump_json(indent=2))


@app.command("memory")
def inspect_memory(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    kind: Annotated[
        str,
        typer.Option(help="Comma-separated working, semantic, or episodic kinds."),
    ] = "",
    query: Annotated[str, typer.Option(help="Recall query; defaults to the task goal.")] = "",
    step: Annotated[
        int,
        typer.Option(help="Exact source step to inspect; -1 disables this filter."),
    ] = -1,
    status: Annotated[
        str,
        typer.Option(help="Comma-separated active, superseded, or invalidated states."),
    ] = "active",
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
) -> None:
    store = _sqlite_store(repo)
    try:
        task = store.get_task(task_id)
        kinds = (
            [MemoryKind(item.strip()) for item in kind.split(",") if item.strip()]
            if kind
            else list(MemoryKind)
        )
        statuses = [MemoryStatus(item.strip()) for item in status.split(",") if item.strip()]
        if not statuses:
            raise ValueError("at least one memory status is required")
        if step < -1:
            raise ValueError("memory step must be -1 or greater")
        exact_step = None if step == -1 else step
        bundle = store.memory.query(
            MemoryQuery(
                task_id=task_id,
                text=query.strip() or task.goal,
                kinds=kinds,
                statuses=statuses,
                step_start=exact_step,
                step_end=exact_step,
                max_results=limit,
                token_budget=max(2_000, limit * 1_000),
            )
        )
    except TaskNotFoundError:
        typer.echo(f"task not found: {task_id}", err=True)
        raise typer.Exit(code=1) from None
    except (MemoryStoreError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    source_map = {source.id: source for source in bundle.sources}
    payload = {
        "task_id": task_id,
        "query": bundle.query.text,
        "filters": {
            "kinds": [item.value for item in bundle.query.kinds],
            "statuses": [item.value for item in bundle.query.statuses],
            "step": exact_step,
            "limit": limit,
        },
        "results": [
            {
                "record": hit.record.model_dump(mode="json"),
                "score": {
                    "total": hit.score,
                    "relevance": hit.relevance_score,
                    "recency": hit.recency_score,
                    "importance": hit.record.importance,
                    "confidence": hit.record.confidence,
                    "source_quality": hit.source_quality_score,
                },
                "why_recalled": hit.reason,
                "matched_terms": hit.matched_terms,
                "sources": [
                    source_map[source_id].model_dump(mode="json")
                    for source_id in hit.record.source_ids
                    if source_id in source_map
                ],
            }
            for hit in bundle.hits
        ],
        "truncated": bundle.truncated,
        "omitted_record_ids": bundle.omitted_record_ids,
    }
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("replay")
def replay_task(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    events = EventLogger(_state_dir(repo) / "traces" / f"{task_id}.jsonl").read()
    if not events:
        typer.echo(f"trace not found: {task_id}", err=True)
        raise typer.Exit(code=1)
    typer.echo(TaskReplay.from_events(task_id, events).model_dump_json(indent=2))


@app.command("context")
def show_context_debug(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    logger = EventLogger(_state_dir(repo) / "traces" / f"{task_id}.jsonl")
    event = next(
        (item for item in reversed(logger.read()) if item.type == "context.built"),
        None,
    )
    if event is None:
        typer.echo(f"context trace not found: {task_id}", err=True)
        raise typer.Exit(code=1)
    debug = ContextDebug.model_validate(event.data["debug"])
    typer.echo(debug.render())
    typer.echo(debug.model_dump_json(indent=2))


@app.command("status")
def task_status(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    services = _workspace_services(repo)
    try:
        task = services.session.task(task_id)
    except (ContractError, KeyError, TaskNotFoundError, ValueError) as exc:
        _command_error(exc)
    if task.session_id is not None:
        _echo_task_result(services, task.session_id, task)
        return
    _echo_json(
        _command_payload(
            services,
            data=task.model_dump(mode="json"),
            task=task,
        )
    )


@app.command("cancel")
def cancel_task(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    services = _workspace_services(repo)
    try:
        task = services.session.cancel_task(task_id)
    except (ContractError, KeyError, TaskNotFoundError, ValueError) as exc:
        _command_error(exc)
    if task.session_id is not None:
        _echo_task_result(services, task.session_id, task)
        return
    _echo_json(
        _command_payload(
            services,
            data=task.model_dump(mode="json"),
            task=task,
        )
    )


@app.command("diff")
def task_diff(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    store = _sqlite_store(repo)
    try:
        task = store.get_task(task_id)
    except TaskNotFoundError:
        typer.echo(f"task not found: {task_id}", err=True)
        raise typer.Exit(code=1) from None
    if task.report is not None:
        typer.echo(task.report.diff or "No changes.")
        return
    try:
        checkpoint = store.get_checkpoint(task_id)
    except TaskNotFoundError:
        typer.echo("No changes.")
        return
    context = ToolContext(repo)
    context.changes.restore(checkpoint.change_snapshot)
    typer.echo(context.changes.diff() or "No changes.")


@app.command("run")
def run_task(
    goal: Annotated[str, typer.Argument(help="Natural-language development goal.")],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    allow_write: Annotated[
        bool,
        typer.Option(help="Allow atomic file creation and exact text replacement."),
    ] = False,
    allow_execute: Annotated[
        bool,
        typer.Option(help="Allow restricted pytest or unittest execution."),
    ] = False,
    max_steps: Annotated[int, typer.Option(min=1, max=1_000)] = 20,
    max_seconds: Annotated[float, typer.Option(min=0.001)] = 300.0,
    max_input_tokens: Annotated[int, typer.Option(min=1)] = 500_000,
    max_output_tokens: Annotated[int, typer.Option(min=1)] = 100_000,
    max_context_tokens: Annotated[int, typer.Option(min=256)] = 32_000,
    max_working_memory_tokens: Annotated[int, typer.Option(min=128)] = 2_000,
    max_tool_output_chars: Annotated[int, typer.Option(min=128)] = 8_000,
    context_recent_steps: Annotated[int, typer.Option(min=1, max=100)] = 4,
    max_cost_usd: Annotated[float, typer.Option(min=0.0001)] = 5.0,
    max_tool_failures: Annotated[int, typer.Option(min=0, max=1_000)] = 10,
    non_interactive: Annotated[
        bool,
        typer.Option(help="Disable prompts; restricted actions still require approval."),
    ] = True,
    sandbox: Annotated[
        str,
        typer.Option(help="Command sandbox backend: docker or local."),
    ] = "docker",
    sandbox_image: Annotated[
        str,
        typer.Option(help="Docker image used by the command sandbox."),
    ] = "patchloop-sandbox:py313",
    sandbox_workspace_limit_mb: Annotated[
        int | None, typer.Option("--sandbox-workspace-limit-mb", min=64, max=16_384)
    ] = None,
    sandbox_workspace_inode_limit: Annotated[
        int, typer.Option("--sandbox-workspace-inode-limit", min=1024)
    ] = 65_536,
    prompt_cache_layout: Annotated[
        str,
        typer.Option(help="Prompt layout: legacy (rollback), stable, or append_only (PPS)."),
    ] = DEFAULT_PROMPT_CACHE_LAYOUT.value,
    append_only_optimization: Annotated[
        str,
        typer.Option(
            "--append-only-optimization",
            help="Append-only optimization: baseline_v1 or balanced_v1.",
        ),
    ] = "baseline_v1",
    provider_profile: Annotated[str | None, typer.Option("--provider")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    provider_config: Annotated[Path | None, typer.Option("--provider-config")] = None,
    env_file: Annotated[Path | None, typer.Option("--env-file")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    events_jsonl: Annotated[bool, typer.Option("--events-jsonl")] = False,
    human: Annotated[bool, typer.Option("--human")] = False,
) -> None:
    repository = repo.resolve()
    permissions = {PermissionLevel.READ}
    if allow_write:
        permissions.add(PermissionLevel.WRITE)
    if allow_execute:
        permissions.add(PermissionLevel.EXECUTE)
    try:
        cache_layout = PromptCacheLayout(prompt_cache_layout)
    except ValueError:
        typer.echo("prompt_cache_layout must be legacy, stable or append_only", err=True)
        raise typer.Exit(code=2) from None
    try:
        optimization = _parse_append_only_optimization(
            append_only_optimization,
            cache_layout,
        )
    except CliUsageError as exc:
        _command_error(exc)
    services = _workspace_services(repository)
    try:
        _validate_sandbox_options(sandbox, sandbox_workspace_limit_mb)
        if sum((json_output, events_jsonl, human)) > 1:
            raise CliUsageError("--json, --events-jsonl, and --human are mutually exclusive")
        load_policy_configuration(services.repository)
        provider, binding = _selected_provider(
            provider_profile,
            model=model,
            config_path=provider_config,
            env_file=env_file,
        )
    except (ValueError, ProviderError) as exc:
        _command_error(exc)
    budget = TaskBudget(
        max_steps=max_steps,
        max_seconds=max_seconds,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_context_tokens=max_context_tokens,
        max_working_memory_tokens=max_working_memory_tokens,
        max_tool_output_chars=max_tool_output_chars,
        context_recent_steps=context_recent_steps,
        max_cost_usd=max_cost_usd,
        max_tool_failures=max_tool_failures,
    )
    execution = TaskExecutionConfig(
        allowed_permissions=sorted(permission.value for permission in permissions),
        non_interactive=non_interactive,
        sandbox_backend=sandbox,
        sandbox_image=sandbox_image,
        sandbox_workspace_limit_mb=sandbox_workspace_limit_mb,
        sandbox_workspace_inode_limit=sandbox_workspace_inode_limit,
        prompt_cache_layout=cache_layout,
        append_only_optimization=optimization,
        provider=binding,
    )
    event_writer: ProviderEventObserver | None
    if events_jsonl:
        event_writer = _ProviderEventWriter()
    elif human:
        event_writer = _HumanProviderWriter()
    else:
        event_writer = None
    try:
        session = services.session.create(str(repository))
        task = services.session.start_task(
            session.id,
            goal,
            budget=budget,
            execution=execution,
        )
        result = _session_runtime_service(
            services,
            task,
            provider=provider,
            provider_event_observer=event_writer,
        ).resume(session.id)
    except (
        ContractError,
        KeyError,
        OSError,
        ProviderError,
        RuntimeError,
        TaskNotFoundError,
        ValueError,
    ) as exc:
        _command_error(exc)
    if result.report is not None and result.outcome is not TaskOutcome.ACTIVE:
        paths = ArtifactStore(_state_dir(repository) / "artifacts").save_report(result)
        for path in paths:
            services.store.record_artifact(task.id, path)
    if events_jsonl:
        assert isinstance(event_writer, _ProviderEventWriter)
        event_writer.flush()
        _echo_json_line(
            {
                "version": CLI_SCHEMA_VERSION,
                "type": "result",
                "request_id": None,
                "attempt_id": None,
                "sequence": services.session.get(session.id).event_sequence,
                "data": _task_presentation(
                    services,
                    session.id,
                    result,
                    services.approval.list(result.id),
                    services.recovery.pending(result.id),
                ),
            }
        )
        if (exit_code := _task_exit_code(result)) is not CliExitCode.SUCCESS:
            raise typer.Exit(code=int(exit_code))
    elif human:
        assert isinstance(event_writer, _HumanProviderWriter)
        event_writer.flush()
        _echo_human_task(result)
        if (exit_code := _task_exit_code(result)) is not CliExitCode.SUCCESS:
            raise typer.Exit(code=int(exit_code))
    else:
        _echo_task_result(services, session.id, result, exit_for_state=True)


@app.command("resume")
def resume_task(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    legacy_provider: Annotated[str | None, typer.Option("--legacy-provider")] = None,
    provider_config: Annotated[Path | None, typer.Option("--provider-config")] = None,
    env_file: Annotated[Path | None, typer.Option("--env-file")] = None,
) -> None:
    repository = repo.resolve()
    services = _workspace_services(repository)
    try:
        task = services.session.task(task_id)
        load_policy_configuration(services.repository, session_id=task.session_id)
        provider = None
        if legacy_provider is not None:
            if task.execution.provider is not None:
                raise CliUsageError("provider override is forbidden for a bound task")
            provider, binding = _configured_provider(
                legacy_provider,
                config_path=provider_config,
                env_file=env_file,
            )
            task = task.model_copy(
                update={"execution": task.execution.model_copy(update={"provider": binding})}
            )
    except (ContractError, KeyError, ProviderError, TaskNotFoundError, ValueError) as exc:
        _command_error(exc)
    if task.status is not TaskStatus.RUNNING:
        _command_error(ValueError(f"task cannot resume from status: {task.status}"))
    try:
        result = _session_runtime_service(services, task, provider=provider).resume_task(task.id)
    except (
        ContractError,
        KeyError,
        OSError,
        ProviderError,
        RuntimeError,
        TaskNotFoundError,
        ValueError,
    ) as exc:
        _command_error(exc)
    if result.report is not None and result.outcome is not TaskOutcome.ACTIVE:
        paths = ArtifactStore(_state_dir(repository) / "artifacts").save_report(result)
        for path in paths:
            services.store.record_artifact(task.id, path)
    if result.session_id is None:
        _command_error(RuntimeError("resumed task was not bound to a Session"))
    _echo_task_result(services, result.session_id, result, exit_for_state=True)
