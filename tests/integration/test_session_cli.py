import json
import multiprocessing
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import patchloop.cli as cli_module
from patchloop.cli import CliExitCode, WorkspaceServices, _task_exit_code, _workspace_services, app
from patchloop.domain import Task, TaskRuntimeCondition, ToolCall
from patchloop.execution.approvals import ApprovalService
from patchloop.execution.effects import arguments_fingerprint
from patchloop.execution.models import ApprovalStatus, ControlKind, Effect, EffectStatus, Execution
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import LeaseGuard
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.runtime import AgentRuntime
from patchloop.security import PolicyDecision
from patchloop.session import SessionService
from patchloop.tools import ListFilesTool, ToolContext, ToolGateway

runner = CliRunner()


def test_session_start_persists_sandbox_capacity_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "patchloop.cli._provider_from_env",
        lambda: FakeProvider([ModelResponse(content="Done")]),
    )
    prefix = ["session", "--repo", str(tmp_path)]
    created = runner.invoke(app, [*prefix, "create"])
    session_id = json.loads(created.stdout)["id"]

    started = runner.invoke(
        app,
        [
            *prefix,
            "start",
            session_id,
            "Inspect",
            "--sandbox-workspace-limit-mb",
            "128",
            "--sandbox-workspace-inode-limit",
            "4096",
        ],
    )

    assert started.exit_code == 0, started.output
    task_id = json.loads(started.stdout)["task_id"]
    persisted = SQLiteStore(tmp_path / ".patchloop" / "patchloop.db").get_task(task_id)
    assert persisted.execution.sandbox_workspace_limit_mb == 128
    assert persisted.execution.sandbox_workspace_inode_limit == 4096


def test_session_start_rejects_local_backend_with_workspace_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "patchloop.cli._provider_from_env",
        lambda: FakeProvider([ModelResponse(content="unused")]),
    )
    prefix = ["session", "--repo", str(tmp_path)]
    created = runner.invoke(app, [*prefix, "create"])
    session_id = json.loads(created.stdout)["id"]

    started = runner.invoke(
        app,
        [
            *prefix,
            "start",
            session_id,
            "Inspect",
            "--sandbox",
            "local",
            "--sandbox-workspace-limit-mb",
            "128",
        ],
    )

    assert started.exit_code == 2
    assert "workspace limits require the Docker sandbox" in started.output


def _run_blocked_session(
    repository_value: str,
    session_id: str,
    ready_value: str,
    release_value: str,
) -> None:
    repository = Path(repository_value)
    ready = Path(ready_value)
    release = Path(release_value)

    class BlockingProvider(FakeProvider):
        def complete(self, messages: object, tools: object = None) -> ModelResponse:
            ready.write_text("ready", encoding="utf-8")
            deadline = time.monotonic() + 15
            while not release.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("test provider was not released")
                time.sleep(0.02)
            return ModelResponse(content="Initial response")

    store = SQLiteStore(repository / ".patchloop" / "patchloop.db")
    runtime = AgentRuntime(
        BlockingProvider([]),
        ToolGateway(ToolContext(repository), [ListFilesTool()]),
        state_store=store,
        owner_id="first-terminal",
    )
    SessionService(store, runtime).resume(session_id)


def _run_cli_process(
    repository: Path, group: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_root = str(Path(__file__).parents[2] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (source_root, environment.get("PYTHONPATH", "")) if value
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "patchloop",
            group,
            "--repo",
            str(repository),
            *arguments,
        ],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def test_cli_registers_session_and_approval_groups() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "session" in result.output
    assert "approval" in result.output


def test_session_group_resolves_explicit_workspace_for_services(tmp_path: Path) -> None:
    services = _workspace_services(tmp_path)
    assert isinstance(services, WorkspaceServices)
    assert services.repository == tmp_path.resolve()
    assert isinstance(services.session, SessionService)
    assert services.session.store.path == tmp_path / ".patchloop" / "patchloop.db"


def test_approval_group_defaults_to_current_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    services = _workspace_services(Path("."))
    expected = Path.cwd().resolve()

    assert isinstance(services, WorkspaceServices)
    assert services.repository == expected
    assert isinstance(services.approval, ApprovalService)
    assert services.approval.store.path == expected / ".patchloop" / "patchloop.db"


def test_session_and_approval_groups_reject_missing_repository(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    session = runner.invoke(app, ["session", "--repo", str(missing), "list"])
    approval = runner.invoke(app, ["approval", "--repo", str(missing), "list", "task"])

    assert session.exit_code == 1
    assert approval.exit_code == 1
    assert json.loads(session.stdout)["error_category"] == "invalid_repository"
    assert json.loads(approval.stdout)["error_category"] == "invalid_repository"
    assert session.stderr == ""
    assert approval.stderr == ""


def test_session_cli_emits_stable_json_errors_and_conflict_exit_code(tmp_path: Path) -> None:
    prefix = ["session", "--repo", str(tmp_path)]

    missing = runner.invoke(app, [*prefix, "show", "missing-session"])

    assert missing.exit_code == 1
    missing_payload = json.loads(missing.stdout)
    assert missing_payload == {
        "schema_version": "1.0",
        "session_id": None,
        "task_id": None,
        "execution_id": None,
        "status": "error",
        "latest_sequence": 0,
        "pending_approvals": [],
        "recovery_items": [],
        "error_category": "not_found",
        "error": {
            "category": "not_found",
            "message": "session not found: missing-session",
            "details": {},
        },
        "next_command": None,
        "next_commands": [],
    }
    assert missing.stderr == ""

    store = SQLiteStore(tmp_path / ".patchloop" / "patchloop.db")
    service = SessionService(store)
    session = service.create(str(tmp_path.resolve()), session_id="session-conflict")
    service.start_task(session.id, "Existing task", task_id="task-existing")
    conflict = runner.invoke(app, [*prefix, "start", session.id, "Second task"])

    assert conflict.exit_code == 12
    conflict_payload = json.loads(conflict.stdout)
    assert conflict_payload["error_category"] == "conflict"
    assert conflict_payload["error"]["details"]["resource_id"] == session.id


def test_competing_resume_returns_machine_readable_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteStore(tmp_path / ".patchloop" / "patchloop.db")
    service = SessionService(store)
    session = service.create(str(tmp_path.resolve()), session_id="session-owned")
    task = service.start_task(
        session.id,
        Task(
            id="task-owned",
            goal="Already running",
            repository=str(tmp_path.resolve()),
            execution={"sandbox_backend": "local"},
        ),
    )
    store.claim_execution(
        Execution(
            id="execution-existing",
            session_id=session.id,
            task_id=task.id,
            owner_id="other-terminal",
            lease_token="other-token",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        ),
        expected_version=task.version,
    )
    monkeypatch.setattr(
        "patchloop.cli._provider_from_env",
        lambda: FakeProvider([ModelResponse(content="must not run")]),
    )

    resumed = runner.invoke(
        app,
        ["session", "--repo", str(tmp_path), "resume", session.id],
    )

    assert resumed.exit_code == CliExitCode.CONFLICT
    payload = json.loads(resumed.stdout)
    assert payload["error_category"] == "conflict"
    assert payload["error"]["details"]["resource_id"] == session.id


@pytest.mark.parametrize(
    ("runtime_condition", "outcome", "expected"),
    [
        ("waiting_for_approval", "active", CliExitCode.WAITING_FOR_APPROVAL),
        ("paused", "active", CliExitCode.PAUSED),
        ("recovery_required", "active", CliExitCode.RECOVERY_REQUIRED),
        ("ended", "failed", CliExitCode.EXECUTION_FAILED),
        ("ended", "cancelled", CliExitCode.CANCELLED),
    ],
)
def test_task_runtime_states_have_distinct_exit_codes(
    runtime_condition: str, outcome: str, expected: CliExitCode
) -> None:
    task = Task(
        goal="Report state",
        repository="workspace",
        status=("running" if outcome == "active" else outcome),
        outcome=outcome,
        runtime_condition=runtime_condition,
    )

    assert _task_exit_code(task) is expected


def test_session_cli_creates_lists_shows_and_closes(tmp_path: Path) -> None:
    prefix = ["session", "--repo", str(tmp_path)]

    created = runner.invoke(app, [*prefix, "create"])
    assert created.exit_code == 0, created.output
    session_id = json.loads(created.stdout)["id"]

    listed = runner.invoke(app, [*prefix, "list"])
    shown = runner.invoke(app, [*prefix, "show", session_id])
    closed = runner.invoke(app, [*prefix, "close", session_id])

    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.stdout)["items"][0]["id"] == session_id
    assert shown.exit_code == 0, shown.output
    shown_payload = json.loads(shown.stdout)
    assert shown_payload["session"]["id"] == session_id
    assert shown_payload["turns"] == []
    assert shown_payload["active_task"] is None
    assert closed.exit_code == 0, closed.output
    assert json.loads(closed.stdout)["status"] == "closed"


def test_session_cli_sends_messages_and_requests_controls(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / ".patchloop" / "patchloop.db")
    service = SessionService(store)
    session = service.create(str(tmp_path.resolve()), session_id="session-control")
    task = service.start_task(session.id, "Keep working", task_id="task-control")
    prefix = ["session", "--repo", str(tmp_path)]

    sent = runner.invoke(
        app,
        [
            *prefix,
            "send",
            session.id,
            "Also update the tests",
            "--client-submission-id",
            "message-1",
        ],
    )
    paused = runner.invoke(app, [*prefix, "pause", session.id])

    assert sent.exit_code == 0, sent.output
    sent_payload = json.loads(sent.stdout)
    assert sent_payload["sequence"] == 1
    assert sent_payload["client_submission_id"] == "message-1"
    assert paused.exit_code == 0, paused.output
    pause_payload = json.loads(paused.stdout)
    assert pause_payload["task_id"] == task.id
    assert pause_payload["kind"] == ControlKind.PAUSE.value

    second = service.create(str(tmp_path.resolve()), session_id="session-cancel")
    service.start_task(second.id, "Stop working", task_id="task-cancel")
    cancelled = runner.invoke(app, [*prefix, "cancel", second.id])

    assert cancelled.exit_code == 0, cancelled.output
    assert json.loads(cancelled.stdout)["kind"] == ControlKind.CANCEL.value


def test_session_enter_persists_messages_and_exit_does_not_cancel(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / ".patchloop" / "patchloop.db")
    service = SessionService(store)
    session = service.create(str(tmp_path.resolve()), session_id="session-enter")
    task = service.start_task(session.id, "Keep working", task_id="task-enter")

    entered = runner.invoke(
        app,
        ["session", "--repo", str(tmp_path), "enter", session.id],
        input="Add a regression test\n/exit\n",
    )

    assert entered.exit_code == 0, entered.output
    assert "message:" not in entered.stdout
    for line in entered.stdout.splitlines():
        assert json.loads(line)["schema_version"] == "1.0"
    assert service.turns(session.id)[0].content == "Add a regression test"
    assert service.active_task(session.id) == task


def test_runtime_ctrl_c_persists_pause_and_waits_for_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteStore(tmp_path / ".patchloop" / "patchloop.db")
    service = SessionService(store)
    session = service.create(str(tmp_path.resolve()), session_id="session-interrupt")
    service.start_task(
        session.id,
        Task(
            id="task-interrupt",
            goal="Interrupt me",
            repository=str(tmp_path.resolve()),
            execution={"sandbox_backend": "local"},
        ),
    )

    class InterruptRuntime:
        def run(self, task: Task) -> Task:
            raise KeyboardInterrupt

        def resume(self, task: Task, checkpoint: object) -> Task:
            raise KeyboardInterrupt

    calls = 0

    def runtime_service(services: WorkspaceServices, task: Task) -> SessionService:
        nonlocal calls
        calls += 1
        if calls == 1:
            return SessionService(store, InterruptRuntime())
        runtime = AgentRuntime(
            FakeProvider([]),
            ToolGateway(ToolContext(tmp_path), [ListFilesTool()]),
            state_store=store,
            owner_id="ctrl-c-cleanup",
        )
        return SessionService(store, runtime)

    monkeypatch.setattr(cli_module, "_session_runtime_service", runtime_service)

    interrupted = runner.invoke(
        app,
        ["session", "--repo", str(tmp_path), "resume", session.id],
    )

    assert interrupted.exit_code == 130, interrupted.output
    payload = json.loads(interrupted.stdout)
    assert payload["interrupted"] is True
    assert payload["control"]["status"] == "settled"
    assert store.get_task("task-interrupt").runtime_condition is TaskRuntimeCondition.PAUSED


def test_enter_ctrl_c_requests_pause_instead_of_normal_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteStore(tmp_path / ".patchloop" / "patchloop.db")
    service = SessionService(store)
    session = service.create(str(tmp_path.resolve()), session_id="session-enter-interrupt")
    service.start_task(session.id, "Keep working", task_id="task-enter-interrupt")
    paused: list[str] = []

    monkeypatch.setattr(
        cli_module.typer, "prompt", lambda *_args, **_kwargs: (_ for _ in ()).throw(typer.Abort())
    )

    def pause_on_interrupt(services: WorkspaceServices, session_id: str) -> None:
        paused.append(session_id)
        raise typer.Exit(code=130)

    monkeypatch.setattr(cli_module, "_pause_interrupted_session", pause_on_interrupt)

    interrupted = runner.invoke(
        app,
        ["session", "--repo", str(tmp_path), "--human", "enter", session.id],
    )

    assert interrupted.exit_code == 130
    assert paused == [session.id]


def test_second_terminal_process_can_send_and_pause_blocked_runtime(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / ".patchloop" / "patchloop.db")
    service = SessionService(store)
    session = service.create(str(tmp_path.resolve()), session_id="session-two-terminals")
    task = service.start_task(
        session.id,
        Task(
            id="task-two-terminals",
            goal="Wait for another terminal",
            repository=str(tmp_path.resolve()),
            execution={"sandbox_backend": "local"},
        ),
    )
    ready = tmp_path / "provider-ready"
    release = tmp_path / "provider-release"
    process = multiprocessing.get_context("spawn").Process(
        target=_run_blocked_session,
        args=(str(tmp_path), session.id, str(ready), str(release)),
    )
    process.start()
    deadline = time.monotonic() + 15
    while not ready.exists() and process.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)

    try:
        assert ready.exists(), "the first terminal did not reach the provider boundary"
        sent = _run_cli_process(
            tmp_path,
            "session",
            "send",
            session.id,
            "Constraint from terminal two",
            "--client-submission-id",
            "terminal-two-message",
        )
        paused = _run_cli_process(tmp_path, "session", "pause", session.id)
        release.write_text("release", encoding="utf-8")
        process.join(timeout=20)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

    assert sent.returncode == 0, sent.stderr
    assert json.loads(sent.stdout)["client_submission_id"] == "terminal-two-message"
    assert paused.returncode == 0, paused.stderr
    control_id = json.loads(paused.stdout)["id"]
    assert process.exitcode == 0
    assert service.control(control_id).status.value == "settled"
    assert store.get_task(task.id).runtime_condition is TaskRuntimeCondition.PAUSED
    assert service.turns(session.id)[0].content == "Constraint from terminal two"


def test_session_start_and_approval_commands_use_persisted_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "README.md").write_text("# Before\n", encoding="utf-8")
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="plan-1",
                        name="update_plan",
                        arguments={
                            "items": [
                                {"description": "Update README", "status": "running"},
                                {"description": "Verify README", "status": "pending"},
                            ]
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="replace-1",
                        name="replace_text",
                        arguments={
                            "path": "README.md",
                            "old_text": "Before",
                            "new_text": "After",
                        },
                    )
                ]
            ),
        ]
    )
    monkeypatch.setattr("patchloop.cli._provider_from_env", lambda: provider)
    prefix = ["session", "--repo", str(tmp_path)]
    created = runner.invoke(app, [*prefix, "create"])
    session_id = json.loads(created.stdout)["id"]

    started = runner.invoke(
        app,
        [*prefix, "start", session_id, "Update README", "--allow-write", "--sandbox", "local"],
    )

    assert started.exit_code == 10, started.output
    task = json.loads(started.stdout)
    assert task["runtime_condition"] == "waiting_for_approval"
    assert task["schema_version"] == "1.0"
    assert task["session_id"] == session_id
    assert task["task_id"] == task["id"]
    assert task["execution_id"] == task["execution"]["id"]
    assert task["status"] == "waiting_for_approval"
    assert task["latest_sequence"] > 0
    assert task["pending_approvals"][0]["status"] == "pending"
    assert task["recovery_items"] == []
    assert task["error_category"] is None
    assert task["next_command"] == task["next_commands"][0]
    assert task["workspace"] == str(tmp_path.resolve())
    assert task["execution"]["owner"].startswith("sha256:")
    assert "lease_token" not in started.stdout
    assert {item["type"] for item in task["activity"]} >= {
        "model.completed",
        "tool.completed",
        "approval.waiting",
    }
    assert task["plan"]["items"][0]["description"] == "Update README"
    assert task["plan_changes"]
    assert task["test_results"] == []
    assert any("approval --repo" in command for command in task["next_commands"])
    assert any("diff" in command for command in task["next_commands"])
    assert any("replay" in command for command in task["next_commands"])
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# Before\n"

    listed = _run_cli_process(tmp_path, "approval", "list", task["id"])
    assert listed.returncode == 0, listed.stderr
    approval = json.loads(listed.stdout)["items"][0]
    assert approval["status"] == ApprovalStatus.PENDING.value

    decided = _run_cli_process(
        tmp_path,
        "approval",
        "decide",
        approval["id"],
        "--approve",
    )
    assert decided.returncode == 0, decided.stderr
    assert json.loads(decided.stdout)["approval"]["status"] == ApprovalStatus.APPROVED.value
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# Before\n"


def test_session_recover_requires_separate_duplicate_risk_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteStore(tmp_path / ".patchloop" / "patchloop.db")
    service = SessionService(store)
    session = service.create(str(tmp_path.resolve()), session_id="session-recovery")
    task = service.start_task(session.id, "Recover external action", task_id="task-recovery")
    execution = store.claim_execution(
        Execution(
            id="execution-recovery",
            session_id=session.id,
            task_id=task.id,
            owner_id="crashed-worker",
            lease_token="recovery-lease",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        ),
        expected_version=task.version,
    )
    guard = LeaseGuard(
        execution.id,
        task.id,
        execution.lease_token,
        execution.generation,
        execution.owner_id,
    )
    arguments = {"path": "external-result.txt", "content": "possibly written\n"}
    prepared = store.prepare_effects(
        [
            Effect(
                id="effect-unknown-cli",
                task_id=task.id,
                step_id="step-unknown-cli",
                batch_position=0,
                provider_call_id="call-unknown-cli",
                tool_name="write_file",
                action_kind="write",
                arguments_summary=arguments,
                arguments_fingerprint=arguments_fingerprint(arguments),
                policy_result={"decision": PolicyDecision.ALLOW.value},
            )
        ],
        expected_version=store.get_task(task.id).version,
        lease_guard=guard,
    )[0]
    claimed = store.claim_effect(
        prepared.id,
        expected_version=prepared.version,
        lease_guard=guard,
    )
    unknown, _ = store.mark_effect_unknown(
        claimed.id,
        expected_version=claimed.version,
        evidence={"source": "worker_crash", "backend_result": "unknown"},
        lease_guard=guard,
    )
    store.release_execution(guard, now=datetime.now(UTC))
    monkeypatch.setattr("patchloop.cli._provider_from_env", lambda: FakeProvider([]))
    prefix = ["session", "--repo", str(tmp_path)]

    shown = runner.invoke(app, [*prefix, "recover", session.id])

    assert shown.exit_code == 0, shown.output
    item = json.loads(shown.stdout)["items"][0]
    assert item["id"] == unknown.id
    assert item["result_state"] == "unknown"
    assert item["retry_may_duplicate_external_side_effect"] is True
    assert item["retry_requires_duplicate_risk_acknowledgement"] is True
    assert "may repeat" in item["warning"]
    assert "--acknowledge-duplicate-risk" in item["next_commands"][1]

    approvals_before = runner.invoke(app, ["approval", "--repo", str(tmp_path), "list", task.id])
    ordinary_approval = runner.invoke(
        app,
        ["approval", "--repo", str(tmp_path), "decide", unknown.id, "--approve"],
    )
    ordinary_resume = runner.invoke(app, [*prefix, "resume", session.id])
    rejected_retry = runner.invoke(
        app,
        [
            *prefix,
            "recover",
            session.id,
            "--effect-id",
            unknown.id,
            "--retry",
            "--evidence",
            '{"operator_note":"retry requested"}',
        ],
    )

    assert approvals_before.exit_code == 0, approvals_before.output
    assert json.loads(approvals_before.stdout)["items"] == []
    assert ordinary_approval.exit_code == int(CliExitCode.EXECUTION_FAILED)
    assert json.loads(ordinary_approval.stdout)["error_category"] == "not_found"
    assert ordinary_resume.exit_code == int(CliExitCode.RECOVERY_REQUIRED)
    assert rejected_retry.exit_code == int(CliExitCode.EXECUTION_FAILED)
    assert "--acknowledge-duplicate-risk" in rejected_retry.stdout
    assert store.list_effects(task.id) == [unknown]

    retried = runner.invoke(
        app,
        [
            *prefix,
            "recover",
            session.id,
            "--effect-id",
            unknown.id,
            "--retry",
            "--acknowledge-duplicate-risk",
            "--evidence",
            '{"operator_note":"duplicate risk reviewed"}',
        ],
    )

    assert retried.exit_code == int(CliExitCode.WAITING_FOR_APPROVAL), retried.output
    payload = json.loads(retried.stdout)
    assert payload["status"] == "waiting_for_approval"
    assert (
        payload["recovery_resolution"]["disposition"]["evidence"]["duplicate_risk_acknowledged"]
        is True
    )
    retry_effects = [effect for effect in store.list_effects(task.id) if effect.id != unknown.id]
    assert len(retry_effects) == 1
    assert retry_effects[0].status is EffectStatus.WAITING_FOR_APPROVAL
    assert retry_effects[0].retry_of_effect_id == unknown.id
