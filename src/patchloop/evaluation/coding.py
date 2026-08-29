"""End-to-end coding task execution with independent test verification."""

from __future__ import annotations

import hashlib
import shutil
import statistics
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from patchloop.domain import Task, TaskBudget, TaskStatus
from patchloop.evaluation.manifest import IGNORED_PARTS, repository_tree_sha256
from patchloop.evaluation.models import Difficulty, TaskType
from patchloop.events import EventLogger
from patchloop.persistence import SQLiteStore
from patchloop.providers.base import ModelProvider
from patchloop.runtime import AgentRuntime
from patchloop.sandbox import CommandSandbox, SandboxError, SandboxTimeoutError
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
)
from patchloop.tools.base import Tool


class CodingTaskDefinition(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    fixture: str
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    goal: str = Field(min_length=1)
    task_type: TaskType
    difficulty: Difficulty
    test_command: list[str] = Field(min_length=1)
    expected_changed_files: list[str] = Field(min_length=1)
    forbidden_changed_files: list[str] = Field(default_factory=list)
    allowed_extra_files: list[str] = Field(default_factory=list)
    max_steps: int = Field(default=24, ge=1, le=100)
    max_cost_usd: float = Field(default=1.0, gt=0.0)


class CodingTaskManifest(BaseModel):
    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")
    suite_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    revision: str = Field(min_length=1)
    tasks: list[CodingTaskDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_tasks(self) -> CodingTaskManifest:
        identifiers = [task.id for task in self.tasks]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("coding task ids must be unique")
        return self


class CodingTaskResult(BaseModel):
    task_id: str
    repeat: int = Field(ge=1)
    success: bool
    agent_status: TaskStatus
    tests_passed: bool
    expected_files_changed: bool
    forbidden_files_unchanged: bool
    only_allowed_files_changed: bool
    changed_files: list[str]
    missing_expected_files: list[str]
    forbidden_changes: list[str]
    unexpected_changes: list[str]
    steps: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    duration_ms: float = Field(ge=0.0)
    verifier_exit_code: int | None = None
    verifier_output: str = ""
    error: str | None = None
    workspace: str
    trace_path: str


class CodingBenchmarkReport(BaseModel):
    suite_id: str
    suite_revision: str
    provider: str
    repeats: int = Field(ge=1)
    total_runs: int = Field(ge=0)
    successful_runs: int = Field(ge=0)
    success_rate: float = Field(ge=0.0, le=1.0)
    test_pass_rate: float = Field(ge=0.0, le=1.0)
    agent_completion_rate: float = Field(ge=0.0, le=1.0)
    mean_duration_ms: float = Field(ge=0.0)
    total_input_tokens: int = Field(ge=0)
    total_output_tokens: int = Field(ge=0)
    total_cost_usd: float = Field(ge=0.0)
    results: list[CodingTaskResult]


def load_coding_manifest(path: Path) -> CodingTaskManifest:
    return CodingTaskManifest.model_validate_json(path.read_text(encoding="utf-8"))


class CodingBenchmarkRunner:
    def __init__(
        self,
        root: Path,
        work_root: Path,
        provider_factory: Callable[[], ModelProvider],
        sandbox_factory: Callable[[], CommandSandbox],
        *,
        repeats: int = 1,
    ) -> None:
        if repeats < 1:
            raise ValueError("coding benchmark repeats must be positive")
        self.root = root.resolve(strict=True)
        self.work_root = work_root.resolve()
        self.provider_factory = provider_factory
        self.sandbox_factory = sandbox_factory
        self.repeats = repeats

    def run(self, manifest: CodingTaskManifest) -> CodingBenchmarkReport:
        first_provider = self.provider_factory()
        run_root = self.work_root / f"run-{uuid4()}"
        run_root.mkdir(parents=True, exist_ok=False)
        results = []
        provider_name = first_provider.name
        first_provider_pending = True
        for repeat in range(1, self.repeats + 1):
            for definition in manifest.tasks:
                if first_provider_pending:
                    provider = first_provider
                    first_provider_pending = False
                else:
                    provider = self.provider_factory()
                results.append(self._run_task(definition, repeat, run_root, provider))
        total = len(results)
        durations = [result.duration_ms for result in results]
        return CodingBenchmarkReport(
            suite_id=manifest.suite_id,
            suite_revision=manifest.revision,
            provider=provider_name,
            repeats=self.repeats,
            total_runs=total,
            successful_runs=sum(result.success for result in results),
            success_rate=sum(result.success for result in results) / total if total else 0.0,
            test_pass_rate=sum(result.tests_passed for result in results) / total if total else 0.0,
            agent_completion_rate=(
                sum(result.agent_status is TaskStatus.COMPLETED for result in results) / total
                if total
                else 0.0
            ),
            mean_duration_ms=statistics.mean(durations) if durations else 0.0,
            total_input_tokens=sum(result.input_tokens for result in results),
            total_output_tokens=sum(result.output_tokens for result in results),
            total_cost_usd=sum(result.cost_usd for result in results),
            results=results,
        )

    def _run_task(
        self,
        definition: CodingTaskDefinition,
        repeat: int,
        run_root: Path,
        provider: ModelProvider,
    ) -> CodingTaskResult:
        source = (self.root / definition.fixture).resolve(strict=True)
        source.relative_to(self.root)
        actual_hash = repository_tree_sha256(source)
        if actual_hash != definition.fixture_sha256:
            raise ValueError(
                f"coding fixture {definition.id} fingerprint mismatch: "
                f"expected {definition.fixture_sha256}, got {actual_hash}"
            )
        workspace = run_root / f"{definition.id}-r{repeat}"
        shutil.copytree(source, workspace)
        original_files = _workspace_file_hashes(workspace)
        state = workspace / ".patchloop"
        trace = EventLogger(state / "trace.jsonl")
        store = SQLiteStore(state / "patchloop.db")
        context = ToolContext(workspace, self.sandbox_factory())
        gateway = ToolGateway(
            context,
            _coding_tools(),
            trace,
            ToolPolicy(
                frozenset({PermissionLevel.READ, PermissionLevel.WRITE, PermissionLevel.EXECUTE})
            ),
        )
        task = Task(
            goal=definition.goal,
            repository=str(workspace),
            budget=TaskBudget(
                max_steps=definition.max_steps,
                max_cost_usd=definition.max_cost_usd,
            ),
        )
        started = perf_counter()
        result = AgentRuntime(provider, gateway, trace, store).run(task)
        verifier_exit_code: int | None = None
        verifier_output = ""
        verification_error: str | None = None
        try:
            command = RunTestsTool._normalize_command(definition.test_command, context)
            verification = self.sandbox_factory().execute(
                command,
                workspace,
                timeout_seconds=120.0,
                max_output_chars=20_000,
            )
            verifier_exit_code = verification.exit_code
            verifier_output = verification.output
        except (OSError, ValueError, SandboxError, SandboxTimeoutError) as exc:
            verification_error = f"{type(exc).__name__}: {exc}"
        duration_ms = (perf_counter() - started) * 1_000
        final_files = _workspace_file_hashes(workspace)
        changed_files = sorted(
            path
            for path in original_files.keys() | final_files.keys()
            if original_files.get(path) != final_files.get(path)
        )
        expected = set(definition.expected_changed_files)
        forbidden = set(definition.forbidden_changed_files)
        allowed = expected | set(definition.allowed_extra_files)
        missing = sorted(expected - set(changed_files))
        forbidden_changes = sorted(forbidden & set(changed_files))
        unexpected_changes = sorted(set(changed_files) - allowed)
        tests_passed = verifier_exit_code == 0
        success = tests_passed and not missing and not forbidden_changes and not unexpected_changes
        report = result.report
        error_parts = [item for item in (result.error, verification_error) if item]
        return CodingTaskResult(
            task_id=definition.id,
            repeat=repeat,
            success=success,
            agent_status=result.status,
            tests_passed=tests_passed,
            expected_files_changed=not missing,
            forbidden_files_unchanged=not forbidden_changes,
            only_allowed_files_changed=not unexpected_changes,
            changed_files=changed_files,
            missing_expected_files=missing,
            forbidden_changes=forbidden_changes,
            unexpected_changes=unexpected_changes,
            steps=len(store.list_steps(task.id)),
            tool_calls=report.tool_calls if report is not None else 0,
            input_tokens=report.input_tokens if report is not None else 0,
            output_tokens=report.output_tokens if report is not None else 0,
            cost_usd=report.cost_usd if report is not None else 0.0,
            duration_ms=duration_ms,
            verifier_exit_code=verifier_exit_code,
            verifier_output=verifier_output[-4_000:],
            error="; ".join(error_parts) or None,
            workspace=str(workspace),
            trace_path=str(trace.path),
        )


def _coding_tools() -> list[Tool]:
    return [
        ListFilesTool(),
        ReadFileTool(),
        SearchTextTool(),
        SearchCodeTool(),
        UpdatePlanTool(),
        CreateFileTool(),
        ApplyPatchTool(),
        ReplaceTextTool(),
        RunCommandTool(),
        RunTestsTool(),
        GetDiffTool(),
    ]


def _workspace_file_hashes(repository: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in repository.rglob("*"):
        relative = path.relative_to(repository)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            hashes[relative.as_posix()] = f"symlink:{path.readlink()}"
        elif path.is_file():
            hashes[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes
