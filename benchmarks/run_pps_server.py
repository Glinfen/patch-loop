"""Run bounded real PPS pairs only after an AOP readiness report is revalidated.

The default command is a read-only preflight. Real Provider requests require
``--execute-real`` plus explicit batch limits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from patchloop.domain import Task, TaskOutcome, TaskRuntimeCondition
from patchloop.evaluation.cache import (
    aop_source_fingerprint,
    append_only_overhead_fixture_fingerprint,
)
from patchloop.evaluation.gates import AopReadinessReport, GateStatus
from patchloop.persistence import SQLiteStore
from patchloop.providers.config import _load_env_file
from patchloop.session import SessionService


@dataclass(frozen=True)
class BatchLimits:
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class TaskLimits:
    max_steps: int
    max_seconds: float
    max_context_tokens: int
    max_input_tokens: int
    max_output_tokens: int
    max_cost_usd: float


@dataclass(frozen=True)
class TrialBudget:
    max_steps: int
    max_seconds: float
    max_context_tokens: int
    max_input_tokens: int
    max_output_tokens: int
    max_cost_usd: float
    max_tool_output_chars: int


@dataclass(frozen=True)
class TrialSpec:
    case: str
    repeat: int
    layout: str

    @property
    def optimization_version(self) -> str:
        return "balanced_v1" if self.layout == "append_only" else "baseline_v1"

    @property
    def name(self) -> str:
        return f"{self.case}-{self.repeat}-{self.layout}"


@dataclass(frozen=True)
class RunnerConfig:
    root: Path
    work_root: Path
    env_file: Path
    readiness_report: Path
    repeats: int
    execute_real: bool
    batch_limits: BatchLimits
    task_limits: TaskLimits
    provider: str | None
    model: str | None
    process_timeout_seconds: float
    inject_inflight_cancel: bool


@dataclass
class BatchUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def allocate(self, config: RunnerConfig, case: str) -> TrialBudget | None:
        input_remaining = config.batch_limits.input_tokens - self.input_tokens
        output_remaining = config.batch_limits.output_tokens - self.output_tokens
        cost_remaining = config.batch_limits.cost_usd - self.cost_usd
        if input_remaining < 1 or output_remaining < 1 or cost_remaining < 0.0001:
            return None
        return TrialBudget(
            max_steps=config.task_limits.max_steps,
            max_seconds=config.task_limits.max_seconds,
            max_context_tokens=config.task_limits.max_context_tokens,
            max_input_tokens=min(config.task_limits.max_input_tokens, input_remaining),
            max_output_tokens=min(config.task_limits.max_output_tokens, output_remaining),
            max_cost_usd=min(config.task_limits.max_cost_usd, cost_remaining),
            max_tool_output_chars=12_000 if case == "long-tool-output" else 1_800,
        )

    def consume(self, outcome: TrialOutcome, limits: BatchLimits) -> str | None:
        if outcome.usage_unknown:
            return "unknown_usage"
        if (
            outcome.input_tokens is None
            or outcome.output_tokens is None
            or outcome.cost_usd is None
        ):
            return "missing_usage"
        self.input_tokens += outcome.input_tokens
        self.output_tokens += outcome.output_tokens
        self.cost_usd += outcome.cost_usd
        if (
            self.input_tokens > limits.input_tokens
            or self.output_tokens > limits.output_tokens
            or self.cost_usd > limits.cost_usd
        ):
            return "batch_budget_exceeded"
        return None


@dataclass(frozen=True)
class TrialOutcome:
    result: dict[str, Any]
    manifest_run: dict[str, Any] | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    usage_unknown: bool
    completed: bool
    stop_reason: str | None = None


TrialExecutor = Callable[
    [RunnerConfig, TrialSpec, TrialBudget, dict[str, Any], dict[str, str], Path],
    TrialOutcome,
]


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preflight or explicitly run a bounded PPS real-provider batch."
    )
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--readiness-report", type=Path, required=True)
    parser.add_argument("--execute-real", action="store_true")
    parser.add_argument("--repeats", type=int, choices=(1, 3), default=1)
    parser.add_argument("--max-batch-input-tokens", type=_positive_int, required=True)
    parser.add_argument("--max-batch-output-tokens", type=_positive_int, required=True)
    parser.add_argument("--max-batch-cost-usd", type=_positive_float, required=True)
    parser.add_argument("--task-max-steps", type=_positive_int, default=40)
    parser.add_argument("--task-max-seconds", type=_positive_float, default=300.0)
    parser.add_argument("--task-max-context-tokens", type=_positive_int, default=24_000)
    parser.add_argument("--task-max-input-tokens", type=_positive_int, default=500_000)
    parser.add_argument("--task-max-output-tokens", type=_positive_int, default=80_000)
    parser.add_argument("--task-max-cost-usd", type=_positive_float, default=1.0)
    parser.add_argument("--process-timeout-seconds", type=_positive_float, default=900.0)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument(
        "--inject-inflight-cancel",
        action="store_true",
        help="Separate fault path: interrupt one active attempt and stop the batch.",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> RunnerConfig:
    root = Path(__file__).resolve().parents[1]
    config = RunnerConfig(
        root=root,
        work_root=args.work_root.resolve(),
        env_file=args.env_file.resolve(),
        readiness_report=args.readiness_report.resolve(),
        repeats=args.repeats,
        execute_real=args.execute_real,
        batch_limits=BatchLimits(
            input_tokens=args.max_batch_input_tokens,
            output_tokens=args.max_batch_output_tokens,
            cost_usd=args.max_batch_cost_usd,
        ),
        task_limits=TaskLimits(
            max_steps=args.task_max_steps,
            max_seconds=args.task_max_seconds,
            max_context_tokens=args.task_max_context_tokens,
            max_input_tokens=args.task_max_input_tokens,
            max_output_tokens=args.task_max_output_tokens,
            max_cost_usd=args.task_max_cost_usd,
        ),
        provider=args.provider,
        model=args.model,
        process_timeout_seconds=args.process_timeout_seconds,
        inject_inflight_cancel=args.inject_inflight_cancel,
    )
    if config.task_limits.max_context_tokens < 256:
        raise ValueError("task max context tokens must be at least 256")
    if config.work_root.exists():
        raise ValueError(f"work root already exists: {config.work_root}")
    if not config.env_file.is_file():
        raise ValueError(f"env file does not exist: {config.env_file}")
    if not (root / "providers.toml").is_file():
        raise ValueError("providers.toml is missing")
    if config.inject_inflight_cancel and not config.execute_real:
        raise ValueError("--inject-inflight-cancel requires --execute-real")
    return config


def _git_revision(root: Path) -> str:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    if not revision:
        raise ValueError("git revision is unavailable")
    return revision


def verify_readiness(path: Path, root: Path) -> AopReadinessReport:
    """Revalidate provenance and every evidence digest before credentials are read."""

    report = AopReadinessReport.model_validate_json(path.read_text(encoding="utf-8"))
    if not report.ready_for_bounded_validation:
        raise ValueError("AOP readiness report does not authorize bounded validation")
    if any(check.status is not GateStatus.PASS for check in report.checks):
        raise ValueError("AOP readiness report contains a non-passing check")
    revision = _git_revision(root)
    if report.revision != revision:
        raise ValueError(
            f"AOP readiness revision drift: expected {report.revision}, current {revision}"
        )
    source_fingerprint = aop_source_fingerprint(root)
    if report.source_fingerprint != source_fingerprint:
        raise ValueError("AOP readiness source fingerprint drift")
    fixture_fingerprint = append_only_overhead_fixture_fingerprint()
    if report.fixture_fingerprint != fixture_fingerprint:
        raise ValueError("AOP readiness fixture fingerprint drift")

    evidence_root = path.parent.resolve(strict=True)
    for evidence in report.evidence_files:
        candidate = (evidence_root / evidence.path).resolve(strict=True)
        try:
            candidate.relative_to(evidence_root)
        except ValueError as exc:
            raise ValueError(f"AOP evidence escapes report directory: {evidence.path}") from exc
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if digest != evidence.sha256:
            raise ValueError(f"AOP evidence digest mismatch: {evidence.path}")
    return report


def _load_scenario(root: Path) -> dict[str, Any]:
    payload = json.loads(
        (root / "benchmarks/real_memory_scenarios.json").read_text(encoding="utf-8")
    )
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios or not isinstance(scenarios[0], dict):
        raise ValueError("real memory scenario manifest is invalid")
    scenario = scenarios[0]
    for key in ("fixture", "hidden_test", "goal"):
        if not isinstance(scenario.get(key), str) or not scenario[key]:
            raise ValueError(f"real memory scenario is missing {key}")
    return scenario


def _trial_specs(repeats: int) -> list[TrialSpec]:
    specs: list[TrialSpec] = []
    for case in ("contract-migration", "long-tool-output"):
        for repeat in range(1, repeats + 1):
            layouts = ("legacy", "append_only") if repeat % 2 else ("append_only", "legacy")
            specs.extend(TrialSpec(case, repeat, layout) for layout in layouts)
    return specs


def _preflight_payload(
    config: RunnerConfig,
    readiness: AopReadinessReport,
    scenario: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "pps-real-preflight.v1",
        "status": "ready" if config.execute_real else "preflight_only",
        "execute_real": config.execute_real,
        "model_requests": 0,
        "revision": readiness.revision,
        "source_fingerprint": readiness.source_fingerprint,
        "fixture_fingerprint": readiness.fixture_fingerprint,
        "provider_config_fingerprint": hashlib.sha256(
            (config.root / "providers.toml").read_bytes()
        ).hexdigest(),
        "optimization_version": readiness.optimization_version,
        "baseline_version": readiness.baseline_version,
        "scenario_fixture": scenario["fixture"],
        "planned_trials": len(_trial_specs(config.repeats)),
        "batch_limits": asdict(config.batch_limits),
        "task_limits": asdict(config.task_limits),
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def execute_batch(
    config: RunnerConfig,
    readiness: AopReadinessReport,
    scenario: dict[str, Any],
    env: dict[str, str],
    *,
    trial_executor: TrialExecutor | None = None,
) -> int:
    """Execute trials sequentially and stop expansion on any uncertain state."""

    executor = trial_executor or _execute_trial
    config.work_root.mkdir(parents=True, exist_ok=False)
    usage = BatchUsage()
    specs = _trial_specs(config.repeats)
    manifest: dict[str, Any] = {
        "schema_version": "pps-real-batch.v2",
        "status": "running",
        "partial_reason": None,
        "revision": readiness.revision,
        "source_fingerprint": readiness.source_fingerprint,
        "fixture_fingerprint": readiness.fixture_fingerprint,
        "provider_config_fingerprint": hashlib.sha256(
            (config.root / "providers.toml").read_bytes()
        ).hexdigest(),
        "optimization_version": readiness.optimization_version,
        "baseline_version": readiness.baseline_version,
        "batch_limits": asdict(config.batch_limits),
        "task_limits": asdict(config.task_limits),
        "runs": [],
    }
    results: list[dict[str, Any]] = []

    def save() -> None:
        manifest["usage"] = asdict(usage)
        _write_json(config.work_root / "manifest.json", manifest)
        _write_json(config.work_root / "results.json", results)

    save()
    for spec in specs:
        budget = usage.allocate(config, spec.case)
        if budget is None:
            manifest["partial_reason"] = "insufficient_remaining_budget"
            break
        try:
            outcome = executor(config, spec, budget, scenario, env, config.work_root)
        except (KeyboardInterrupt, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            results.append({"trial": spec.name, "error": str(exc), "partial": True})
            manifest["partial_reason"] = f"trial_interrupted:{spec.name}"
            save()
            break
        results.append(outcome.result)
        if outcome.manifest_run is not None:
            manifest["runs"].append(outcome.manifest_run)
        reason = outcome.stop_reason or usage.consume(outcome, config.batch_limits)
        if reason is None and not outcome.completed:
            reason = "task_or_validation_incomplete"
        if reason is not None:
            manifest["partial_reason"] = f"{reason}:{spec.name}"
            save()
            break
        save()

    completed = len(results) == len(specs) and manifest["partial_reason"] is None
    manifest["status"] = "completed" if completed else "partial"
    save()
    print(json.dumps({"status": manifest["status"], "work_root": str(config.work_root)}))
    return 0 if completed else 1


def _prepare_trial_repository(
    config: RunnerConfig,
    spec: TrialSpec,
    scenario: dict[str, Any],
    env: dict[str, str],
    trial: Path,
) -> Path:
    repo = trial / "workspace"
    shutil.copytree(config.root / "benchmarks" / scenario["fixture"], repo)
    if spec.case == "long-tool-output":
        for evidence in sorted((repo / "evidence").glob("*.md"))[4:]:
            with evidence.open("a", encoding="utf-8") as handle:
                handle.write("\nHistorical log noise; no contract changes.\n" * 500)
    (repo / ".gitignore").write_text(
        ".patchloop/\n__pycache__/\n.pytest_cache/\n", encoding="utf-8"
    )
    for command in (
        ["git", "init", "-q"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=PPS Fixture",
            "-c",
            "user.email=pps@localhost",
            "commit",
            "-qm",
            "fixture:固定验收初始状态",
        ],
    ):
        subprocess.run(command, cwd=repo, env=env, check=True)
    return repo


def _build_run_command(
    config: RunnerConfig,
    spec: TrialSpec,
    budget: TrialBudget,
    scenario: dict[str, Any],
    repo: Path,
) -> list[str]:
    goal = str(scenario["goal"]) + (
        " Use separate tool rounds to read each evidence file in order. "
        "Record a plan before editing and before running tests."
    )
    command = [
        sys.executable,
        "-m",
        "patchloop",
        "run",
        goal,
        "--repo",
        str(repo),
        "--provider-config",
        str(config.root / "providers.toml"),
        "--env-file",
        str(config.env_file),
        "--prompt-cache-layout",
        spec.layout,
        "--append-only-optimization",
        spec.optimization_version,
        "--allow-write",
        "--allow-execute",
        "--sandbox",
        "local",
        "--max-steps",
        str(budget.max_steps),
        "--max-seconds",
        str(budget.max_seconds),
        "--max-context-tokens",
        str(budget.max_context_tokens),
        "--max-input-tokens",
        str(budget.max_input_tokens),
        "--max-output-tokens",
        str(budget.max_output_tokens),
        "--max-tool-output-chars",
        str(budget.max_tool_output_chars),
        "--max-cost-usd",
        str(budget.max_cost_usd),
        "--json",
    ]
    if config.provider is not None:
        command.extend(["--provider", config.provider])
    if config.model is not None:
        command.extend(["--model", config.model])
    return command


def _trace_state(trace: Path) -> tuple[int, set[str]]:
    completed = 0
    active: set[str] = set()
    seen: set[str] = set()
    for line in trace.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # A writer may be between bytes.  Treat that window as active so a
            # quiet pause is never requested from incomplete trace evidence.
            active.add("unparseable-trace-line")
            continue
        event_id = event.get("id")
        if isinstance(event_id, str) and event_id in seen:
            continue
        if isinstance(event_id, str):
            seen.add(event_id)
        event_type = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        attempt_id = data.get("attempt_id")
        if event_type == "provider.attempt.started" and isinstance(attempt_id, str):
            active.add(attempt_id)
        elif event_type == "provider.attempt.finished" and isinstance(attempt_id, str):
            active.discard(attempt_id)
        elif event_type == "provider.request.completed":
            if isinstance(attempt_id, str):
                active.discard(attempt_id)
            completed += 1
    return completed, active


def _request_quiet_pause(repo: Path, trace: Path, request_id: str) -> bool:
    completed, active = _trace_state(trace)
    if completed < 2 or active:
        return False
    store = SQLiteStore(repo / ".patchloop/patchloop.db")
    task = store.get_task(trace.stem)
    if task.session_id is None:
        raise RuntimeError("PPS task has no Session for pause/resume")
    SessionService(store).request_pause(task.session_id, request_id=request_id)
    return True


def _trace_has_unsettled_attempts(task: Task, active_attempts: set[str]) -> bool:
    return bool(active_attempts) and task.runtime_condition is not TaskRuntimeCondition.ENDED


def _resume(
    repo: Path,
    env: dict[str, str],
    session_id: str,
    stdout_path: Path,
    stderr_path: Path,
    timeout: float,
) -> int:
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "patchloop",
                "session",
                "--repo",
                str(repo),
                "--json",
                "resume",
                session_id,
            ],
            cwd=repo,
            env=env,
            stdout=stdout,
            stderr=stderr,
            timeout=timeout,
        )
    return result.returncode


def _approval_is_permitted(approval: Any, effect: Any, repo: Path) -> bool:
    return bool(
        (
            effect.tool_name in {"apply_patch", "replace_text", "write_file"}
            and approval.resource_summary == f"workspace={repo}; paths=order_service.py"
        )
        or (
            effect.tool_name == "run_command"
            and approval.resource_summary == f"workspace={repo}"
            and effect.arguments_summary.get("command")
            in (
                ["git", "status", "--short"],
                ["git", "diff", "--", "order_service.py"],
            )
        )
        or (
            effect.tool_name == "run_tests"
            and approval.resource_summary == f"workspace={repo}"
            and effect.arguments_summary.get("command")
            in (
                ["python", "-m", "pytest"],
                ["python", "-m", "pytest", "-q"],
                ["pytest"],
                ["pytest", "-q"],
            )
        )
    )


def _approve_and_resume(
    config: RunnerConfig,
    repo: Path,
    trial: Path,
    env: dict[str, str],
    task: Task,
) -> tuple[list[str], int | None, str | None]:
    if task.session_id is None:
        return [], None, "task_missing_session"
    session_id = task.session_id
    store = SQLiteStore(repo / ".patchloop/patchloop.db")
    approved_ids: list[str] = []
    resume_code: int | None = None
    for approval_round in range(4):
        task = store.get_task(task.id)
        pending = [
            approval
            for approval in store.list_approvals(task.id)
            if approval.status.value == "pending"
        ]
        if not pending:
            return approved_ids, resume_code, None
        if not all(
            _approval_is_permitted(approval, store.get_effect(approval.effect_id), repo)
            for approval in pending
        ):
            return approved_ids, resume_code, "approval_scope_rejected"
        for approval in pending:
            with (trial / f"approval-{approval.id}.json").open(
                "w", encoding="utf-8"
            ) as stdout:
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "patchloop",
                        "approval",
                        "--repo",
                        str(repo),
                        "decide",
                        approval.id,
                        "--approve",
                        "--source",
                        "pps-fixture-operator",
                    ],
                    cwd=repo,
                    env=env,
                    stdout=stdout,
                    check=True,
                )
            approved_ids.append(approval.id)
        resume_code = _resume(
            repo,
            env,
            session_id,
            trial / f"approved-{approval_round}.stdout",
            trial / f"approved-{approval_round}.stderr",
            config.process_timeout_seconds,
        )
    return approved_ids, resume_code, "approval_round_limit"


def _execute_trial(
    config: RunnerConfig,
    spec: TrialSpec,
    budget: TrialBudget,
    scenario: dict[str, Any],
    env: dict[str, str],
    work: Path,
) -> TrialOutcome:
    trial = work / spec.name
    repo = _prepare_trial_repository(config, spec, scenario, env, trial)
    command = _build_run_command(config, spec, budget, scenario, repo)
    strategy = {
        "trial": spec.name,
        "case": spec.case,
        "repeat": spec.repeat,
        "layout": spec.layout,
        "optimization_version": spec.optimization_version,
        "provider": config.provider,
        "model": config.model,
        "provider_config_fingerprint": hashlib.sha256(
            (config.root / "providers.toml").read_bytes()
        ).hexdigest(),
        "budget": asdict(budget),
        "scenario": {
            "fixture": scenario["fixture"],
            "hidden_test": scenario["hidden_test"],
            "goal": scenario["goal"],
        },
        "execution_policy": {
            "sandbox": "local",
            "write_scope": ["order_service.py"],
            "allowed_read_only_commands": [
                ["git", "status", "--short"],
                ["git", "diff", "--", "order_service.py"],
            ],
            "allowed_test_commands": [
                ["python", "-m", "pytest"],
                ["python", "-m", "pytest", "-q"],
                ["pytest"],
                ["pytest", "-q"],
            ],
            "required_changed_files": ["order_service.py"],
            "required_validations": ["public", "hidden"],
        },
        "command": command,
    }
    _write_json(trial / "trial_manifest.json", strategy)
    print(f"START {spec.name}", flush=True)
    pause_requested = False
    cancel_injected = False
    timed_out = False
    with (trial / "run.stdout").open("w", encoding="utf-8") as stdout, (
        trial / "run.stderr"
    ).open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(command, cwd=repo, env=env, stdout=stdout, stderr=stderr)
        deadline = time.monotonic() + config.process_timeout_seconds
        while process.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                break
            traces = list((repo / ".patchloop/traces").glob("*.jsonl"))
            trace = traces[0] if traces else None
            if trace is not None and config.inject_inflight_cancel and not cancel_injected:
                _, active = _trace_state(trace)
                if active:
                    process.send_signal(signal.SIGINT)
                    cancel_injected = True
            elif (
                trace is not None
                and spec.layout == "append_only"
                and spec.repeat == 1
                and spec.case == "contract-migration"
                and not pause_requested
            ):
                pause_requested = _request_quiet_pause(
                    repo, trace, f"pps-quiet-pause-{spec.name}"
                )
            time.sleep(0.2)

    traces = list((repo / ".patchloop/traces").glob("*.jsonl"))
    if not traces:
        return TrialOutcome(
            result={
                **strategy,
                "exit_code": process.returncode,
                "partial": True,
                "reason": "missing_trace",
            },
            manifest_run=None,
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
            usage_unknown=True,
            completed=False,
            stop_reason="missing_trace",
        )
    trace = traces[0]
    store = SQLiteStore(repo / ".patchloop/patchloop.db")
    task = store.get_task(trace.stem)
    paused_state_verified = False
    resume_code: int | None = None
    if pause_requested:
        paused_state_verified = task.runtime_condition is TaskRuntimeCondition.PAUSED
        if task.session_id is None:
            raise RuntimeError("paused PPS task has no Session")
        resume_code = _resume(
            repo,
            env,
            task.session_id,
            trial / "resume.stdout",
            trial / "resume.stderr",
            config.process_timeout_seconds,
        )
        task = store.get_task(task.id)

    approved_ids, approval_resume_code, approval_reason = _approve_and_resume(
        config, repo, trial, env, task
    )
    if approval_resume_code is not None:
        resume_code = approval_resume_code
    task = store.get_task(task.id)

    validation: dict[str, int | None] = {"public": None, "hidden": None}
    if task.outcome is TaskOutcome.COMPLETED:
        for name, test in (
            ("public", str(repo)),
            ("hidden", str(config.root / "benchmarks" / scenario["hidden_test"])),
        ):
            outcome = subprocess.run(
                [sys.executable, "-m", "pytest", test, "-q"],
                cwd=repo,
                env={**env, "PATCHLOOP_REAL_MEMORY_REPO": str(repo)},
                text=True,
                capture_output=True,
                timeout=120,
            )
            (trial / f"{name}.txt").write_text(
                outcome.stdout + outcome.stderr, encoding="utf-8"
            )
            validation[name] = outcome.returncode
    changed = subprocess.check_output(
        ["git", "diff", "--name-only"], cwd=repo, text=True
    ).splitlines()
    (trial / "changes.diff").write_text(
        subprocess.check_output(["git", "diff"], cwd=repo, text=True), encoding="utf-8"
    )

    report = task.report
    _, active_attempts = _trace_state(trace)
    usage_unknown = (
        report is None
        or report.unknown_usage_attempts > 0
        or report.unknown_model_usage_calls > 0
        or not report.model_usage_exact
        or report.cost_status == "unknown"
        or _trace_has_unsettled_attempts(task, active_attempts)
    )
    completed = (
        task.outcome is TaskOutcome.COMPLETED
        and task.runtime_condition is TaskRuntimeCondition.ENDED
        and validation == {"public": 0, "hidden": 0}
        and changed == ["order_service.py"]
        and approval_reason is None
        and not timed_out
        and not cancel_injected
    )
    stop_reason = (
        "inflight_cancel_fault"
        if cancel_injected
        else "trial_timeout"
        if timed_out
        else approval_reason
    )
    result = {
        **strategy,
        "approved_ids": approved_ids,
        "exit_code": process.returncode,
        "resume_code": resume_code,
        "pause_requested": pause_requested,
        "pause_verified": paused_state_verified,
        "inflight_cancel_injected": cancel_injected,
        "task_status": task.status.value,
        "task_outcome": task.outcome.value,
        "runtime_condition": task.runtime_condition.value,
        "validation": validation,
        "changed_files": changed,
        "usage_unknown": usage_unknown,
        "input_tokens": None if report is None else report.input_tokens,
        "output_tokens": None if report is None else report.output_tokens,
        "cost_usd": None if report is None or usage_unknown else report.cost_usd,
        "completed": completed,
    }
    manifest_run = {
        "trace": str(trace.relative_to(work)),
        "task_id": task.id,
        "variant": "current_layout" if spec.layout == "legacy" else "append_only",
        "repeat": spec.repeat,
        "batch_id": work.name,
        "task_case": spec.case,
        "pair_id": f"{spec.case}-{spec.repeat}",
        "strategy": strategy,
        "task_budget": task.budget.model_dump(mode="json"),
        "task_execution": task.execution.model_dump(mode="json"),
        "initial_exit_code": process.returncode,
        "resume_exit_code": resume_code,
        "approval_ids": approved_ids,
        "task_outcome": task.outcome.value,
        "runtime_condition": task.runtime_condition.value,
        "validation": validation,
    }
    print("DONE " + json.dumps(result), flush=True)
    return TrialOutcome(
        result=result,
        manifest_run=manifest_run,
        input_tokens=None if report is None else report.input_tokens,
        output_tokens=None if report is None else report.output_tokens,
        cost_usd=None if report is None or usage_unknown else report.cost_usd,
        usage_unknown=usage_unknown,
        completed=completed,
        stop_reason=stop_reason,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        config = _config_from_args(args)
        readiness = verify_readiness(config.readiness_report, config.root)
        scenario = _load_scenario(config.root)
        print(json.dumps(_preflight_payload(config, readiness, scenario)))
        if not config.execute_real:
            return 0
        env = {**_load_env_file(config.env_file), **os.environ}
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        return execute_batch(config, readiness, scenario, env)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
