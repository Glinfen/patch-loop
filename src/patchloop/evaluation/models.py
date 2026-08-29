"""Versioned evaluation manifests and machine-readable benchmark results."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, model_validator

from patchloop.domain import utc_now


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class TaskType(StrEnum):
    BUGFIX = "bugfix"
    FEATURE = "feature"
    TEST = "test"
    DOCUMENTATION = "documentation"


class EvaluationVariant(StrEnum):
    SINGLE_SHOT = "single_shot"
    NO_PLAN = "no_plan"
    TEXT_ONLY = "text_only"
    PATCHLOOP = "patchloop"


class RepositoryDefinition(BaseModel):
    path: str
    revision: str = Field(min_length=1)
    tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    setup_command: list[str] = Field(default_factory=list)
    test_command: list[str] = Field(default_factory=list)


class SuccessCriteria(BaseModel):
    expected_paths: list[str] = Field(min_length=1)
    minimum_path_recall: float = Field(default=1.0, ge=0.0, le=1.0)


class EvaluationTask(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    repository: str
    goal: str = Field(min_length=1)
    query: str = Field(min_length=1)
    task_type: TaskType
    difficulty: Difficulty
    k: int = Field(default=3, ge=1, le=20)
    criteria: SuccessCriteria


class EvaluationManifest(BaseModel):
    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")
    suite_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    revision: str = Field(min_length=1)
    repositories: dict[str, RepositoryDefinition] = Field(min_length=1)
    tasks: list[EvaluationTask] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        identifiers = [task.id for task in self.tasks]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("evaluation task ids must be unique")
        missing = sorted(
            {task.repository for task in self.tasks if task.repository not in self.repositories}
        )
        if missing:
            raise ValueError(f"unknown task repositories: {', '.join(missing)}")
        return self


class EvaluationCandidate(BaseModel):
    selected_paths: list[str] = Field(default_factory=list)
    plan_steps: list[str] = Field(default_factory=list)
    notes: str = ""
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)


class EvaluationAttempt(BaseModel):
    attempt: int = Field(ge=1)
    passed: bool
    path_recall: float = Field(ge=0.0, le=1.0)
    selected_paths: list[str] = Field(default_factory=list)
    plan_steps: int = Field(default=0, ge=0)
    duration_ms: float = Field(default=0.0, ge=0.0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    error: str | None = None


class EvaluationTaskResult(BaseModel):
    task_id: str
    task_type: TaskType
    difficulty: Difficulty
    passed: bool
    attempts: list[EvaluationAttempt] = Field(min_length=1)


class EvaluationAggregate(BaseModel):
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    success_rate: float = Field(ge=0.0, le=1.0)
    total_attempts: int = Field(ge=0)
    mean_latency_ms: float = Field(default=0.0, ge=0.0)
    p95_latency_ms: float = Field(default=0.0, ge=0.0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_cost_usd: float = Field(default=0.0, ge=0.0)
    by_difficulty: dict[str, float] = Field(default_factory=dict)
    by_task_type: dict[str, float] = Field(default_factory=dict)


class EvaluationReport(BaseModel):
    suite_id: str
    suite_revision: str
    variant: EvaluationVariant
    repository_revisions: dict[str, str]
    aggregate: EvaluationAggregate
    results: list[EvaluationTaskResult]
    generated_at: datetime = Field(default_factory=utc_now)


class EvaluationSuiteReport(BaseModel):
    suite_id: str
    suite_revision: str
    reports: list[EvaluationReport]
    generated_at: datetime = Field(default_factory=utc_now)
