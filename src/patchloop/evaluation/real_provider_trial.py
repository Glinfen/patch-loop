"""SRF-07 reproducible real-repository and real-provider trial runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.domain import utc_now


class TrialStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNVERIFIED = "unverified"


class CommandLocation(StrEnum):
    WORKSPACE = "workspace"
    RUNNER = "runner"


class TrialCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(min_length=1)
    location: CommandLocation
    timeout_seconds: float = Field(default=300.0, gt=0.0)


class ProviderLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    model: str
    credential_environment: list[str] = Field(min_length=1)


class RepositoryLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    python: str
    dependencies: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def dependencies_are_exact(self) -> Self:
        if any("==" not in dependency for dependency in self.dependencies):
            raise ValueError("real trial dependencies must use exact == versions")
        return self


class TrialIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    title: str
    source: str
    goal: str
    additional_constraint: str


class RealProviderTrialManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    provider: ProviderLock
    repository: RepositoryLock
    issue: TrialIssue
    trial_count: int = Field(default=3, ge=3)
    max_resume_attempts: int = Field(default=12, ge=1)
    public_validation: list[TrialCommand] = Field(min_length=1)
    independent_validation: list[TrialCommand] = Field(min_length=1)

    @model_validator(mode="after")
    def validations_use_expected_locations(self) -> Self:
        if any(item.location is not CommandLocation.WORKSPACE for item in self.public_validation):
            raise ValueError("public validation must run inside the trial workspace")
        if any(item.location is not CommandLocation.RUNNER for item in self.independent_validation):
            raise ValueError("independent validation must run outside the trial workspace")
        return self


class CommandEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str]
    location: CommandLocation
    exit_code: int | None
    duration_ms: float = Field(ge=0.0)
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


class UsageEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    unknown_remote_usage_calls: int = Field(default=0, ge=0)
    exact: bool = False


class RealProviderTrialResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trial: int = Field(ge=1)
    status: TrialStatus
    stage: str
    workspace: str
    expected_revision: str
    actual_revision: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    constraint_turn_id: str | None = None
    approval_ids: list[str] = Field(default_factory=list)
    restart_count: int = Field(default=0, ge=0)
    diff: str = ""
    diff_sha256: str | None = None
    usage: UsageEvidence = Field(default_factory=UsageEvidence)
    duration_ms: float = Field(ge=0.0)
    commands: list[CommandEvidence] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)


class RealProviderTrialReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    manifest_schema_version: int
    status: TrialStatus
    provider: str
    model: str
    repository_url: str
    revision: str
    issue_id: str
    dependency_lock: list[str]
    credential_environment: list[str]
    credential_configured: bool
    results: list[RealProviderTrialResult]
    generated_at: str = Field(default_factory=lambda: utc_now().isoformat())

    @model_validator(mode="after")
    def aggregate_is_consistent(self) -> Self:
        statuses = {result.status for result in self.results}
        expected = (
            TrialStatus.FAILED
            if TrialStatus.FAILED in statuses
            else TrialStatus.UNVERIFIED
            if TrialStatus.UNVERIFIED in statuses
            else TrialStatus.PASSED
        )
        if self.status is not expected:
            raise ValueError("real-provider trial aggregate status is inconsistent")
        return self


class RealProviderTrialRunner:
    """Run all samples and retain every outcome rather than selecting a best run."""

    def __init__(self, root: Path, work_root: Path, *, environment: dict[str, str] | None = None):
        self.root = root.resolve(strict=True)
        self.work_root = work_root.resolve()
        self.environment = dict(os.environ if environment is None else environment)
        self._secrets: list[str] = []
        self._credential_names: list[str] = []
        self._provider_model = ""

    def run(self, manifest: RealProviderTrialManifest) -> RealProviderTrialReport:
        self._credential_names = list(manifest.provider.credential_environment)
        self._provider_model = manifest.provider.model
        self._secrets = [
            value
            for name in manifest.provider.credential_environment
            if (value := self.environment.get(name, "").strip())
        ]
        if not self._secrets:
            detail = "real Provider credential is not configured; trial was not executed"
            results = [
                self._unverified_result(manifest, trial, detail)
                for trial in range(1, manifest.trial_count + 1)
            ]
        elif shutil.which("git") is None or shutil.which("uv") is None:
            detail = "git and uv are required to create locked clean trial workspaces"
            results = [
                self._unverified_result(manifest, trial, detail)
                for trial in range(1, manifest.trial_count + 1)
            ]
        else:
            self.work_root.mkdir(parents=True, exist_ok=True)
            results = [
                self._run_trial(manifest, trial) for trial in range(1, manifest.trial_count + 1)
            ]
        statuses = {result.status for result in results}
        status = (
            TrialStatus.FAILED
            if TrialStatus.FAILED in statuses
            else TrialStatus.UNVERIFIED
            if TrialStatus.UNVERIFIED in statuses
            else TrialStatus.PASSED
        )
        return RealProviderTrialReport(
            manifest_schema_version=manifest.schema_version,
            status=status,
            provider=manifest.provider.name,
            model=manifest.provider.model,
            repository_url=manifest.repository.url,
            revision=manifest.repository.revision,
            issue_id=manifest.issue.id,
            dependency_lock=manifest.repository.dependencies,
            credential_environment=manifest.provider.credential_environment,
            credential_configured=bool(self._secrets),
            results=results,
        )

    def _unverified_result(
        self, manifest: RealProviderTrialManifest, trial: int, detail: str
    ) -> RealProviderTrialResult:
        return RealProviderTrialResult(
            trial=trial,
            status=TrialStatus.UNVERIFIED,
            stage="preflight",
            workspace=str(self.work_root / f"trial-{trial:02d}"),
            expected_revision=manifest.repository.revision,
            duration_ms=0.0,
            failures=[detail],
        )

    def _run_trial(
        self, manifest: RealProviderTrialManifest, trial: int
    ) -> RealProviderTrialResult:
        started = perf_counter()
        workspace = self.work_root / f"trial-{trial:02d}"
        result = RealProviderTrialResult(
            trial=trial,
            status=TrialStatus.FAILED,
            stage="workspace",
            workspace=str(workspace),
            expected_revision=manifest.repository.revision,
            duration_ms=0.0,
        )
        if workspace.exists():
            result.failures.append("trial workspace already exists; refusing to overwrite evidence")
            return self._finish(result, started)
        clone = self._command(
            ["git", "clone", "--no-checkout", manifest.repository.url, str(workspace)],
            self.root,
            timeout=300.0,
        )
        result.commands.append(clone)
        if clone.exit_code != 0:
            result.failures.append("repository clone failed")
            return self._finish(result, started)
        for argv in (
            ["git", "checkout", "--detach", manifest.repository.revision],
            ["git", "rev-parse", "HEAD"],
        ):
            evidence = self._command(argv, workspace)
            result.commands.append(evidence)
            if evidence.exit_code != 0:
                result.failures.append("locked revision checkout failed")
                return self._finish(result, started)
        result.actual_revision = result.commands[-1].stdout.strip()
        if result.actual_revision != manifest.repository.revision:
            result.failures.append("checked out revision does not match manifest")
            return self._finish(result, started)

        result.stage = "dependencies"
        trial_python = self._trial_python(workspace)
        setup_commands = [
            ["uv", "venv", str(workspace / ".venv"), "--python", manifest.repository.python],
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(trial_python),
                "-e",
                str(workspace),
                *manifest.repository.dependencies,
            ],
        ]
        for argv in setup_commands:
            evidence = self._command(argv, workspace, timeout=600.0)
            result.commands.append(evidence)
            if evidence.exit_code != 0:
                result.failures.append("locked dependency setup failed")
                return self._finish(result, started)

        result.stage = "session"
        created = self._patchloop(
            workspace, ["session", "--repo", str(workspace), "--json", "create"]
        )
        result.commands.append(created)
        create_payload = self._json_payload(created)
        result.session_id = _optional_text(create_payload.get("session_id"))
        if created.exit_code != 0 or result.session_id is None:
            result.failures.append("Session creation failed")
            return self._finish(result, started)

        initial = self._patchloop(
            workspace,
            [
                "session",
                "--repo",
                str(workspace),
                "--json",
                "start",
                result.session_id,
                manifest.issue.goal,
                "--allow-write",
                "--allow-execute",
                "--sandbox",
                "local",
            ],
            timeout=900.0,
        )
        result.commands.append(initial)
        initial_payload = self._json_payload(initial)
        result.task_id = _optional_text(initial_payload.get("task_id"))
        if result.task_id is None:
            result.failures.append("initial Provider process did not create a Task")
            return self._finish(result, started)
        if initial_payload.get("status") != "waiting_for_approval":
            result.failures.append("initial process did not stop at the required approval boundary")
            return self._finish(result, started)

        constraint = self._patchloop(
            workspace,
            [
                "session",
                "--repo",
                str(workspace),
                "--json",
                "send",
                result.session_id,
                manifest.issue.additional_constraint,
                "--client-submission-id",
                f"srf07-{manifest.issue.id}-{trial}",
            ],
        )
        result.commands.append(constraint)
        constraint_payload = self._json_payload(constraint)
        result.constraint_turn_id = _optional_text(constraint_payload.get("id"))
        if constraint.exit_code != 0 or result.constraint_turn_id is None:
            result.failures.append("additional constraint was not persisted")
            return self._finish(result, started)

        result.stage = "approval_and_restart"
        terminal_payload = initial_payload
        for _ in range(manifest.max_resume_attempts):
            pending = terminal_payload.get("pending_approvals", [])
            if not isinstance(pending, list):
                result.failures.append("CLI returned malformed pending approvals")
                return self._finish(result, started)
            for item in pending:
                approval_id = _optional_text(item.get("id")) if isinstance(item, dict) else None
                if approval_id is None:
                    result.failures.append("CLI returned an approval without an id")
                    return self._finish(result, started)
                decision = self._patchloop(
                    workspace,
                    [
                        "approval",
                        "--repo",
                        str(workspace),
                        "--json",
                        "decide",
                        approval_id,
                        "--approve",
                        "--source",
                        "srf07-real-provider-trial",
                    ],
                )
                result.commands.append(decision)
                if decision.exit_code != 0:
                    result.failures.append(f"approval decision failed: {approval_id}")
                    return self._finish(result, started)
                result.approval_ids.append(approval_id)
            result.restart_count += 1
            resumed = self._patchloop(
                workspace,
                ["session", "--repo", str(workspace), "--json", "resume", result.session_id],
                timeout=900.0,
            )
            result.commands.append(resumed)
            terminal_payload = self._json_payload(resumed)
            status = terminal_payload.get("status")
            if status == "completed":
                break
            if status != "waiting_for_approval":
                result.failures.append(f"resume stopped in unexpected state: {status}")
                return self._finish(result, started)
        else:
            result.failures.append("maximum approval/resume attempts exceeded")
            return self._finish(result, started)

        result.stage = "validation"
        placeholders = {
            "workspace": str(workspace),
            "python": str(trial_python),
            "runner_python": sys.executable,
            "root": str(self.root),
        }
        for command in [*manifest.public_validation, *manifest.independent_validation]:
            argv = [item.format_map(placeholders) for item in command.argv]
            cwd = workspace if command.location is CommandLocation.WORKSPACE else self.root
            evidence = self._command(argv, cwd, timeout=command.timeout_seconds)
            result.commands.append(evidence)
            if evidence.exit_code != 0:
                result.failures.append(f"{command.location.value} validation failed")

        diff_evidence = self._command(["git", "diff", "--no-ext-diff", "--"], workspace)
        result.commands.append(diff_evidence)
        result.diff = diff_evidence.stdout
        result.diff_sha256 = hashlib.sha256(result.diff.encode()).hexdigest()
        metrics = self._patchloop(workspace, ["metrics", result.task_id, "--repo", str(workspace)])
        result.commands.append(metrics)
        metrics_payload = self._json_payload(metrics)
        unknown_calls = int(metrics_payload.get("unknown_model_usage_calls", 0) or 0)
        result.usage = UsageEvidence(
            input_tokens=int(metrics_payload.get("input_tokens", 0) or 0),
            output_tokens=int(metrics_payload.get("output_tokens", 0) or 0),
            cost_usd=float(metrics_payload.get("cost_usd", 0.0) or 0.0),
            unknown_remote_usage_calls=unknown_calls,
            exact=metrics.exit_code == 0 and unknown_calls == 0,
        )
        if not result.approval_ids or result.restart_count < 1 or not result.diff:
            result.failures.append("required approval, restart, or diff evidence is missing")
        result.status = TrialStatus.PASSED if not result.failures else TrialStatus.FAILED
        result.stage = "complete" if result.status is TrialStatus.PASSED else result.stage
        return self._finish(result, started)

    def _patchloop(
        self, workspace: Path, arguments: list[str], *, timeout: float = 300.0
    ) -> CommandEvidence:
        environment = self.environment.copy()
        environment["PATH"] = os.pathsep.join(
            [str(self._trial_python(workspace).parent), environment.get("PATH", "")]
        )
        environment["DEEPSEEK_MODEL"] = self._provider_model
        environment["LLM_MODEL_ID"] = self._provider_model
        source = str(self.root / "src")
        environment["PYTHONPATH"] = os.pathsep.join(
            item for item in [source, environment.get("PYTHONPATH", "")] if item
        )
        return self._command(
            [sys.executable, "-m", "patchloop", *arguments],
            workspace,
            timeout=timeout,
            environment=environment,
        )

    def _command(
        self,
        argv: list[str],
        cwd: Path,
        *,
        timeout: float = 120.0,
        environment: dict[str, str] | None = None,
    ) -> CommandEvidence:
        started = perf_counter()
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=self._safe_environment() if environment is None else environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandEvidence(
                argv=self._redact_argv(argv),
                location=CommandLocation.WORKSPACE if cwd != self.root else CommandLocation.RUNNER,
                exit_code=None,
                duration_ms=(perf_counter() - started) * 1_000,
                error=self._redact(f"{type(exc).__name__}: {exc}"),
            )
        return CommandEvidence(
            argv=self._redact_argv(argv),
            location=CommandLocation.WORKSPACE if cwd != self.root else CommandLocation.RUNNER,
            exit_code=completed.returncode,
            duration_ms=(perf_counter() - started) * 1_000,
            stdout=self._redact(completed.stdout),
            stderr=self._redact(completed.stderr),
        )

    @staticmethod
    def _json_payload(evidence: CommandEvidence) -> dict[str, Any]:
        try:
            payload = json.loads(evidence.stdout)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _redact(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    def _redact_argv(self, argv: list[str]) -> list[str]:
        return [self._redact(item) for item in argv]

    def _safe_environment(self) -> dict[str, str]:
        return {
            name: value
            for name, value in self.environment.items()
            if name not in self._credential_names
        }

    @staticmethod
    def _trial_python(workspace: Path) -> Path:
        scripts = "Scripts" if os.name == "nt" else "bin"
        executable = "python.exe" if os.name == "nt" else "python"
        return workspace / ".venv" / scripts / executable

    @staticmethod
    def _finish(result: RealProviderTrialResult, started: float) -> RealProviderTrialResult:
        result.duration_ms = (perf_counter() - started) * 1_000
        return result


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def load_real_provider_trial_manifest(path: Path) -> RealProviderTrialManifest:
    return RealProviderTrialManifest.model_validate_json(path.read_text(encoding="utf-8"))


def write_real_provider_trial_report(report: RealProviderTrialReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SRF-07 real Provider trials")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    arguments = parser.parse_args()
    manifest = load_real_provider_trial_manifest(arguments.manifest)
    report = RealProviderTrialRunner(arguments.root, arguments.work_root).run(manifest)
    write_real_provider_trial_report(report, arguments.output)
    print(report.model_dump_json(indent=2))
    if report.status is TrialStatus.PASSED:
        return 0
    if report.status is TrialStatus.FAILED:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
