from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import run_pps_server as runner
from patchloop.domain import Task, TaskRuntimeCondition
from patchloop.evaluation.cache import (
    aop_source_fingerprint,
    append_only_overhead_fixture_fingerprint,
)
from patchloop.evaluation.gates import (
    AopEvidenceFile,
    AopReadinessReport,
    CacheGateCheck,
    GateStatus,
)
from patchloop.persistence import SQLiteStore
from patchloop.session import SessionService


def _readiness(tmp_path: Path) -> Path:
    root = Path(__file__).parents[2]
    evidence = tmp_path / "overhead.json"
    evidence.write_text('{"offline":true}', encoding="utf-8")
    report = AopReadinessReport(
        revision=runner._git_revision(root),
        source_fingerprint=aop_source_fingerprint(root),
        fixture_fingerprint=append_only_overhead_fixture_fingerprint(),
        checks=[
            CacheGateCheck(
                name="offline",
                status=GateStatus.PASS,
                actual=True,
                target="pass",
                detail="threshold satisfied",
            )
        ],
        evidence_files=[
            AopEvidenceFile(
                path=evidence.name,
                sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),
            )
        ],
        ready_for_bounded_validation=True,
    )
    path = tmp_path / "readiness.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def _arguments(tmp_path: Path, readiness: Path, *, execute: bool = False) -> list[str]:
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=must-not-be-read-during-preflight\n", encoding="utf-8")
    values = [
        "--work-root",
        str(tmp_path / "work"),
        "--env-file",
        str(env_file),
        "--readiness-report",
        str(readiness),
        "--max-batch-input-tokens",
        "1000",
        "--max-batch-output-tokens",
        "500",
        "--max-batch-cost-usd",
        "2",
    ]
    if execute:
        values.append("--execute-real")
    return values


def test_default_command_is_read_only_preflight(tmp_path, monkeypatch, capsys):
    readiness = _readiness(tmp_path)
    monkeypatch.setattr(
        runner,
        "_load_env_file",
        lambda _: (_ for _ in ()).throw(AssertionError("credentials loaded")),
    )
    monkeypatch.setattr(
        runner,
        "execute_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("batch executed")),
    )

    code = runner.main(_arguments(tmp_path, readiness))

    assert code == 0
    assert not (tmp_path / "work").exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "preflight_only"
    assert payload["execute_real"] is False
    assert payload["model_requests"] == 0
    assert payload["planned_trials"] == 4
    assert len(payload["provider_config_fingerprint"]) == 64


@pytest.mark.parametrize("failure", ["missing", "source_drift", "evidence_drift"])
def test_failed_readiness_stops_before_credentials_or_tasks(
    tmp_path, monkeypatch, capsys, failure
):
    readiness = _readiness(tmp_path)
    if failure == "missing":
        readiness = tmp_path / "missing.json"
    elif failure == "source_drift":
        payload = json.loads(readiness.read_text(encoding="utf-8"))
        payload["source_fingerprint"] = "0" * 64
        readiness.write_text(json.dumps(payload), encoding="utf-8")
    else:
        (tmp_path / "overhead.json").write_text("drift", encoding="utf-8")
    loaded = False
    executed = False

    def load(_):
        nonlocal loaded
        loaded = True
        return {}

    def execute(*args, **kwargs):
        nonlocal executed
        executed = True
        return 0

    monkeypatch.setattr(runner, "_load_env_file", load)
    monkeypatch.setattr(runner, "execute_batch", execute)

    code = runner.main(_arguments(tmp_path, readiness, execute=True))

    assert code == 2
    assert loaded is executed is False
    assert not (tmp_path / "work").exists()
    assert capsys.readouterr().err


def test_invalid_budget_is_rejected_before_credentials(tmp_path, monkeypatch):
    readiness = _readiness(tmp_path)
    loaded = False

    def load(_):
        nonlocal loaded
        loaded = True
        return {}

    monkeypatch.setattr(runner, "_load_env_file", load)
    arguments = _arguments(tmp_path, readiness)
    arguments[arguments.index("1000")] = "0"

    with pytest.raises(SystemExit) as exc:
        runner.main(arguments)

    assert exc.value.code == 2
    assert loaded is False


def _config(tmp_path: Path, readiness: Path) -> runner.RunnerConfig:
    root = Path(__file__).parents[2]
    return runner.RunnerConfig(
        root=root,
        work_root=tmp_path / "batch",
        env_file=tmp_path / ".env",
        readiness_report=readiness,
        repeats=1,
        execute_real=True,
        batch_limits=runner.BatchLimits(input_tokens=100, output_tokens=50, cost_usd=1),
        task_limits=runner.TaskLimits(
            max_steps=20,
            max_seconds=60,
            max_context_tokens=8_000,
            max_input_tokens=80,
            max_output_tokens=40,
            max_cost_usd=0.8,
        ),
        provider="test-provider",
        model="fixed-model",
        process_timeout_seconds=90,
        inject_inflight_cancel=False,
    )


def test_unknown_usage_stops_batch_and_saves_partial_report(tmp_path):
    readiness_path = _readiness(tmp_path)
    readiness = AopReadinessReport.model_validate_json(
        readiness_path.read_text(encoding="utf-8")
    )
    config = _config(tmp_path, readiness_path)
    calls: list[str] = []

    def execute(config, spec, budget, scenario, env, work):
        del config, budget, scenario, env, work
        calls.append(spec.name)
        return runner.TrialOutcome(
            result={"trial": spec.name, "exit_code": 0},
            manifest_run={"trial": spec.name},
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
            usage_unknown=True,
            completed=True,
        )

    code = runner.execute_batch(
        config,
        readiness,
        {"fixture": "unused", "hidden_test": "unused", "goal": "unused"},
        {},
        trial_executor=execute,
    )

    assert code == 1
    assert calls == ["contract-migration-1-legacy"]
    manifest = json.loads((config.work_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "partial"
    assert manifest["partial_reason"].startswith("unknown_usage:")
    assert len(manifest["runs"]) == 1


def test_true_task_state_not_exit_code_stops_batch(tmp_path):
    readiness_path = _readiness(tmp_path)
    readiness = AopReadinessReport.model_validate_json(
        readiness_path.read_text(encoding="utf-8")
    )
    config = _config(tmp_path, readiness_path)

    def execute(config, spec, budget, scenario, env, work):
        del config, budget, scenario, env, work
        return runner.TrialOutcome(
            result={"trial": spec.name, "exit_code": 0, "task_outcome": "active"},
            manifest_run={"trial": spec.name},
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.1,
            usage_unknown=False,
            completed=False,
        )

    code = runner.execute_batch(
        config,
        readiness,
        {"fixture": "unused", "hidden_test": "unused", "goal": "unused"},
        {},
        trial_executor=execute,
    )

    assert code == 1
    manifest = json.loads((config.work_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["partial_reason"].startswith("task_or_validation_incomplete:")


def test_quiet_pause_requires_no_live_attempt_and_persists_control(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(repository / ".patchloop/patchloop.db")
    service = SessionService(store)
    session = service.create(str(repository), session_id="session-1")
    service.start_task(session.id, "Inspect fixture", task_id="task-1")
    trace_root = repository / ".patchloop/traces"
    trace_root.mkdir(parents=True)
    trace = trace_root / "task-1.jsonl"
    active_events = [
        {
            "id": "start-1",
            "type": "provider.attempt.started",
            "data": {"attempt_id": "completed-1"},
        },
        {
            "id": "complete-1",
            "type": "provider.request.completed",
            "data": {"attempt_id": "completed-1"},
        },
        {
            "id": "complete-2",
            "type": "provider.request.completed",
            "data": {"attempt_id": "completed-2"},
        },
        {
            "id": "attempt-1",
            "type": "provider.attempt.started",
            "data": {"attempt_id": "active"},
        },
    ]
    trace.write_text(
        "\n".join(json.dumps(event) for event in active_events) + "\n", encoding="utf-8"
    )

    assert runner._request_quiet_pause(repository, trace, "pause-1") is False

    active_events.append(
        {
            "id": "attempt-2",
            "type": "provider.request.completed",
            "data": {"attempt_id": "active"},
        }
    )
    trace.write_text(
        "\n".join(json.dumps(event) for event in active_events) + "\n", encoding="utf-8"
    )
    assert runner._request_quiet_pause(repository, trace, "pause-1") is True
    assert store.get_control_request("pause-1").status.value == "requested"


def test_completed_task_uses_durable_usage_after_trace_flush_lag():
    ended = SimpleNamespace(runtime_condition=TaskRuntimeCondition.ENDED)
    running = SimpleNamespace(runtime_condition=TaskRuntimeCondition.RUNNING)

    assert not runner._trace_has_unsettled_attempts(ended, {"attempt-1"})
    assert runner._trace_has_unsettled_attempts(running, {"attempt-1"})


def test_acceptance_pause_only_uses_durable_approval_boundary():
    spec = runner.TrialSpec("contract-migration", 1, "append_only")
    task = Task(id="task-1", goal="Inspect", repository="workspace")
    task.transition_runtime(TaskRuntimeCondition.RUNNING)

    assert not runner._is_quiet_pause_boundary(spec, task)

    task.transition_runtime(TaskRuntimeCondition.WAITING_FOR_APPROVAL)

    assert runner._is_quiet_pause_boundary(spec, task)


def test_trial_command_carries_complete_strategy_and_remaining_budget(tmp_path):
    readiness = _readiness(tmp_path)
    config = _config(tmp_path, readiness)
    budget = runner.BatchUsage(input_tokens=30, output_tokens=20, cost_usd=0.4).allocate(
        config, "contract-migration"
    )
    assert budget is not None
    command = runner._build_run_command(
        config,
        runner.TrialSpec("contract-migration", 1, "append_only"),
        budget,
        {"goal": "Repair fixture"},
        tmp_path,
    )

    assert command[command.index("--append-only-optimization") + 1] == "balanced_v1"
    assert command[command.index("--max-input-tokens") + 1] == "70"
    assert command[command.index("--max-output-tokens") + 1] == "30"
    assert command[command.index("--max-cost-usd") + 1] == "0.6"
    assert command[command.index("--max-seconds") + 1] == "60"
    assert command[command.index("--provider") + 1] == "test-provider"
    assert command[command.index("--model") + 1] == "fixed-model"


def test_approval_scope_remains_exact(tmp_path):
    repository = tmp_path.resolve()
    approval = SimpleNamespace(resource_summary=f"workspace={repository}; paths=order_service.py")
    apply_patch = SimpleNamespace(tool_name="apply_patch", arguments_summary={})
    replace_text = SimpleNamespace(tool_name="replace_text", arguments_summary={})
    write_file = SimpleNamespace(tool_name="write_file", arguments_summary={})
    broad = SimpleNamespace(tool_name="apply_patch", arguments_summary={})

    assert runner._approval_is_permitted(approval, apply_patch, repository)
    assert runner._approval_is_permitted(approval, replace_text, repository)
    assert runner._approval_is_permitted(approval, write_file, repository)
    approval.resource_summary = f"workspace={repository}; paths=order_service.py,other.py"
    assert not runner._approval_is_permitted(approval, broad, repository)


def test_approval_scope_allows_only_fixture_read_commands(tmp_path):
    repository = tmp_path.resolve()
    approval = SimpleNamespace(resource_summary=f"workspace={repository}")

    for command in (
        ["git", "status", "--short"],
        ["git", "diff"],
        ["git", "diff", "--name-only"],
        ["git", "diff", "--", "order_service.py"],
    ):
        effect = SimpleNamespace(tool_name="run_command", arguments_summary={"command": command})
        assert runner._approval_is_permitted(approval, effect, repository)

    unsupported = SimpleNamespace(
        tool_name="run_command", arguments_summary={"command": ["git", "show"]}
    )
    assert not runner._approval_is_permitted(approval, unsupported, repository)


def test_approval_round_boundary_accepts_no_remaining_pending_requests():
    pending = SimpleNamespace(status=SimpleNamespace(value="pending"))
    approved = SimpleNamespace(status=SimpleNamespace(value="approved"))
    store = SimpleNamespace(list_approvals=lambda _task_id: [approved])

    assert runner._pending_approvals(store, "task-1") == []

    store.list_approvals = lambda _task_id: [approved, pending]

    assert runner._pending_approvals(store, "task-1") == [pending]
