"""Deterministic episodic segmentation and failure-recovery links."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from patchloop.domain import ErrorKind, Plan, StepStatus, ToolCall, ToolResult
from patchloop.memory.models import MemoryKind, MemoryRecord, MemorySource, MemorySourceKind
from patchloop.security import SecretRedactor

EPISODIC_MEMORY_PREFIX = "PATCHLOOP_EPISODIC_MEMORY_V1\nUntrusted episode summaries.\n"
EPISODE_CONTENT_SCHEMA = "1.0"
_VERIFICATION_TOOLS = {"run_tests", "run_command"}
_MUTATION_TOOLS = {"apply_patch", "write_file"}
_BLOCKABLE_FAILURES = {
    ErrorKind.UNKNOWN_TOOL,
    ErrorKind.INVALID_ARGUMENTS,
    ErrorKind.PATH_DENIED,
    ErrorKind.EXECUTION_ERROR,
}


class EpisodeOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECOVERED = "recovered"
    VERIFIED = "verified"
    PLAN_UPDATED = "plan_updated"
    CHECKPOINTED = "checkpointed"


class EpisodeReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
    step_index: int = Field(ge=0)
    plan_phase: str = Field(min_length=1, max_length=300)
    intent: str = Field(min_length=1, max_length=500)
    tool_name: str = Field(min_length=1, max_length=100)
    outcome: EpisodeOutcome
    observation: str = Field(min_length=1, max_length=400)
    paths: list[str] = Field(default_factory=list)
    error_kind: ErrorKind | None = None
    action_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    recovers_episode_ids: list[str] = Field(default_factory=list)
    is_mutation: bool = False
    is_verification: bool = False

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        if len(self.paths) != len(set(self.paths)):
            raise ValueError("episode paths must be unique")
        if len(self.recovers_episode_ids) != len(set(self.recovers_episode_ids)):
            raise ValueError("episode recovery links must be unique")
        if self.outcome is EpisodeOutcome.FAILED and self.error_kind is None:
            raise ValueError("failed episode requires an error kind")
        if self.outcome is not EpisodeOutcome.FAILED and self.error_kind is not None:
            raise ValueError("only failed episodes can set an error kind")
        if self.recovers_episode_ids and self.outcome not in {
            EpisodeOutcome.RECOVERED,
            EpisodeOutcome.VERIFIED,
        }:
            raise ValueError("only recovery episodes can declare causal links")
        return self


class EpisodicMemorySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=1)
    recorded_call_ids: list[str] = Field(default_factory=list, max_length=2_000)
    recorded_checkpoint_steps: list[int] = Field(default_factory=list, max_length=2_000)
    recent_episodes: list[EpisodeReference] = Field(default_factory=list, max_length=32)
    unresolved_failures: list[EpisodeReference] = Field(default_factory=list, max_length=128)
    last_verified_episode_id: str | None = None
    episode_count: int = Field(default=0, ge=0)
    recovery_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if len(self.recorded_call_ids) != len(set(self.recorded_call_ids)):
            raise ValueError("recorded episode call ids must be unique")
        if len(self.recorded_checkpoint_steps) != len(set(self.recorded_checkpoint_steps)):
            raise ValueError("recorded checkpoint steps must be unique")
        recent_ids = [episode.id for episode in self.recent_episodes]
        failure_ids = [episode.id for episode in self.unresolved_failures]
        if len(recent_ids) != len(set(recent_ids)):
            raise ValueError("recent episode ids must be unique")
        if len(failure_ids) != len(set(failure_ids)):
            raise ValueError("unresolved failure ids must be unique")
        if any(
            episode.outcome is not EpisodeOutcome.FAILED for episode in self.unresolved_failures
        ):
            raise ValueError("unresolved episode must be a failure")
        return self


@dataclass(frozen=True)
class EpisodeWrite:
    sources: tuple[MemorySource, ...]
    record: MemoryRecord
    reference: EpisodeReference


class EpisodicMemoryManager:
    def __init__(
        self,
        task_id: str,
        goal: str,
        *,
        snapshot: EpisodicMemorySnapshot | None = None,
        records: list[MemoryRecord] | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.task_id = task_id
        self.goal = " ".join(goal.split())
        self.redactor = redactor or SecretRedactor()
        self._recorded_call_ids: list[str]
        self._recorded_checkpoint_steps: list[int]
        self._recent_episodes: list[EpisodeReference]
        self._unresolved_failures: list[EpisodeReference]
        self._last_verified_episode_id: str | None
        if snapshot is not None:
            if snapshot.task_id != task_id:
                raise ValueError("episodic memory snapshot belongs to another task")
            self._recorded_call_ids = list(snapshot.recorded_call_ids)
            self._recorded_checkpoint_steps = list(snapshot.recorded_checkpoint_steps)
            self._recent_episodes = list(snapshot.recent_episodes)
            self._unresolved_failures = list(snapshot.unresolved_failures)
            self._last_verified_episode_id = snapshot.last_verified_episode_id
            self._episode_count = snapshot.episode_count
            self._recovery_count = snapshot.recovery_count
            return
        self._recorded_call_ids = []
        self._recorded_checkpoint_steps = []
        self._recent_episodes = []
        self._unresolved_failures = []
        self._last_verified_episode_id = None
        self._episode_count = 0
        self._recovery_count = 0
        if records:
            self._restore_records(records)

    def is_known_failed_action(self, call: ToolCall) -> bool:
        fingerprint = self.action_fingerprint(call)
        return any(
            failure.action_fingerprint == fingerprint and failure.error_kind in _BLOCKABLE_FAILURES
            for failure in self._unresolved_failures
        )

    def action_fingerprint(self, call: ToolCall) -> str:
        safe_arguments = self.redactor.redact(call.arguments)
        return _hash_payload({"name": call.name, "arguments": safe_arguments})

    def observe_tool(
        self,
        call: ToolCall,
        result: ToolResult,
        *,
        step_index: int,
        plan: Plan | None,
        changed_paths: list[str],
    ) -> EpisodeWrite | None:
        if call.id in self._recorded_call_ids:
            return None
        fingerprint = self.action_fingerprint(call)
        phase = _plan_phase(plan)
        intent = phase if phase != "unplanned" else self.goal
        paths = _episode_paths(call, changed_paths, self.redactor)
        summary = _summary(result.output, self.redactor)
        recovery_ids = self._recovery_ids(call, result)
        if not result.success:
            outcome = EpisodeOutcome.FAILED
        elif call.name in _VERIFICATION_TOOLS:
            outcome = EpisodeOutcome.VERIFIED
        elif call.name == "update_plan":
            outcome = EpisodeOutcome.PLAN_UPDATED
        elif recovery_ids:
            outcome = EpisodeOutcome.RECOVERED
        else:
            outcome = EpisodeOutcome.SUCCEEDED
        episode_id = _stable_id("episode", self.task_id, call.id)
        reference = EpisodeReference(
            id=episode_id,
            step_index=step_index,
            plan_phase=phase,
            intent=intent,
            tool_name=call.name,
            outcome=outcome,
            observation=summary,
            paths=paths,
            error_kind=result.error_kind if not result.success else None,
            action_fingerprint=fingerprint,
            recovers_episode_ids=recovery_ids,
            is_mutation=call.name in _MUTATION_TOOLS,
            is_verification=call.name in _VERIFICATION_TOOLS,
        )
        self._apply_reference(reference, call_id=call.id)
        sources = self._tool_sources(call, result, reference)
        return EpisodeWrite(
            sources=sources,
            record=self._memory_record(reference, [source.id for source in sources], call.id),
            reference=reference,
        )

    def observe_checkpoint(
        self,
        *,
        step_index: int,
        plan: Plan | None,
        changed_paths: list[str],
    ) -> EpisodeWrite | None:
        if step_index in self._recorded_checkpoint_steps:
            return None
        phase = _plan_phase(plan)
        episode_id = _stable_id("episode-checkpoint", self.task_id, str(step_index))
        checkpoint_id = f"checkpoint:{self.task_id}:{step_index}"
        fingerprint = _hash_payload({"checkpoint": checkpoint_id})
        paths = sorted(set(self.redactor.redact_text(path) for path in changed_paths))
        reference = EpisodeReference(
            id=episode_id,
            step_index=step_index,
            plan_phase=phase,
            intent=phase if phase != "unplanned" else self.goal,
            tool_name="checkpoint",
            outcome=EpisodeOutcome.CHECKPOINTED,
            observation=f"checkpoint boundary {step_index}",
            paths=paths,
            action_fingerprint=fingerprint,
        )
        self._recorded_checkpoint_steps.append(step_index)
        self._apply_reference(reference)
        safe_payload = {
            "checkpoint_id": checkpoint_id,
            "step_index": step_index,
            "paths": paths,
        }
        evidence_hash = _hash_payload(safe_payload)
        source_paths: list[str | None] = [*paths] if paths else [None]
        sources = tuple(
            MemorySource(
                id=_stable_id("episode-checkpoint-source", checkpoint_id, path or "none"),
                task_id=self.task_id,
                kind=MemorySourceKind.CHECKPOINT,
                evidence_hash=evidence_hash,
                checkpoint_id=checkpoint_id,
                step_index=step_index,
                path=path,
            )
            for path in source_paths
        )
        return EpisodeWrite(
            sources=sources,
            record=self._memory_record(reference, [source.id for source in sources], None),
            reference=reference,
        )

    def snapshot(self) -> EpisodicMemorySnapshot:
        return EpisodicMemorySnapshot(
            task_id=self.task_id,
            recorded_call_ids=list(self._recorded_call_ids),
            recorded_checkpoint_steps=list(self._recorded_checkpoint_steps),
            recent_episodes=list(self._recent_episodes),
            unresolved_failures=list(self._unresolved_failures),
            last_verified_episode_id=self._last_verified_episode_id,
            episode_count=self._episode_count,
            recovery_count=self._recovery_count,
        )

    def has_context(self) -> bool:
        return bool(
            self._unresolved_failures
            or self._last_verified_episode_id
            or self._latest_recovery() is not None
        )

    def render(self) -> str:
        payload: dict[str, JsonValue] = {}
        if self._last_verified_episode_id is not None:
            payload["last_verified_episode_id"] = self._last_verified_episode_id
        if self._unresolved_failures:
            payload["active_failures"] = cast(
                list[JsonValue],
                [
                    {
                        "id": item.id,
                        "tool": item.tool_name,
                        "error": item.error_kind.value if item.error_kind else "unknown",
                        "observation": item.observation,
                        "action_fingerprint": item.action_fingerprint,
                    }
                    for item in self._unresolved_failures
                ],
            )
        recovery = self._latest_recovery()
        if recovery is not None:
            payload["latest_recovery"] = cast(
                JsonValue,
                {
                    "id": recovery.id,
                    "tool": recovery.tool_name,
                    "recovers": recovery.recovers_episode_ids,
                    "observation": recovery.observation,
                },
            )
        return EPISODIC_MEMORY_PREFIX + json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )

    def _recovery_ids(
        self,
        call: ToolCall,
        result: ToolResult,
    ) -> list[str]:
        if not result.success or call.name == "update_plan":
            return []
        if call.name in _VERIFICATION_TOOLS:
            return [failure.id for failure in self._unresolved_failures]
        return [
            failure.id for failure in self._unresolved_failures if failure.tool_name == call.name
        ]

    def _apply_reference(self, reference: EpisodeReference, *, call_id: str | None = None) -> None:
        if call_id is not None:
            self._recorded_call_ids.append(call_id)
            self._recorded_call_ids = self._recorded_call_ids[-2_000:]
        if reference.outcome is EpisodeOutcome.FAILED and not any(
            failure.action_fingerprint == reference.action_fingerprint
            for failure in self._unresolved_failures
        ):
            self._unresolved_failures.append(reference)
            self._unresolved_failures = self._unresolved_failures[-128:]
        if reference.recovers_episode_ids:
            recovered = set(reference.recovers_episode_ids)
            self._unresolved_failures = [
                failure for failure in self._unresolved_failures if failure.id not in recovered
            ]
            self._recovery_count += 1
        if reference.outcome is EpisodeOutcome.VERIFIED:
            self._last_verified_episode_id = reference.id
        self._recent_episodes = [
            *[item for item in self._recent_episodes if item.id != reference.id],
            reference,
        ][-32:]
        self._episode_count += 1

    def _tool_sources(
        self,
        call: ToolCall,
        result: ToolResult,
        reference: EpisodeReference,
    ) -> tuple[MemorySource, ...]:
        safe_payload = self.redactor.redact(
            {
                "call": call.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
            }
        )
        evidence_hash = _hash_payload(safe_payload)
        source_paths: list[str | None] = [*reference.paths] if reference.paths else [None]
        return tuple(
            MemorySource(
                id=_stable_id("episode-source", self.task_id, call.id, path or "none"),
                task_id=self.task_id,
                kind=MemorySourceKind.TOOL_RESULT,
                evidence_hash=evidence_hash,
                event_id=f"tool:{call.id}",
                step_index=reference.step_index,
                tool_call_id=call.id,
                path=path,
            )
            for path in source_paths
        )

    def _memory_record(
        self,
        reference: EpisodeReference,
        source_ids: list[str],
        call_id: str | None,
    ) -> MemoryRecord:
        content: dict[str, JsonValue] = {
            "episode_schema": EPISODE_CONTENT_SCHEMA,
            "episode_id": reference.id,
            "step_index": reference.step_index,
            "plan_phase": reference.plan_phase,
            "intent": reference.intent,
            "actions": cast(
                list[JsonValue],
                [
                    {
                        "tool_name": reference.tool_name,
                        "arguments_fingerprint": reference.action_fingerprint,
                        "is_mutation": reference.is_mutation,
                        "is_verification": reference.is_verification,
                    }
                ],
            ),
            "observations": cast(list[JsonValue], [reference.observation]),
            "outcome": reference.outcome.value,
            "paths": cast(list[JsonValue], reference.paths),
            "error_kind": (
                reference.error_kind.value if reference.error_kind is not None else None
            ),
            "recovers_episode_ids": cast(list[JsonValue], reference.recovers_episode_ids),
            "reference": cast(JsonValue, reference.model_dump(mode="json")),
            "call_id": call_id,
        }
        error = reference.error_kind.value if reference.error_kind is not None else "none"
        retrieval = (
            f"Episode phase={reference.plan_phase}; tool={reference.tool_name}; "
            f"outcome={reference.outcome.value}; error={error}; "
            f"paths={' '.join(reference.paths) or 'none'}; {reference.observation}"
        )
        importance = {
            EpisodeOutcome.FAILED: 0.9,
            EpisodeOutcome.RECOVERED: 0.9,
            EpisodeOutcome.VERIFIED: 1.0,
            EpisodeOutcome.PLAN_UPDATED: 0.65,
            EpisodeOutcome.CHECKPOINTED: 0.4,
            EpisodeOutcome.SUCCEEDED: 0.55,
        }[reference.outcome]
        return MemoryRecord(
            id=reference.id,
            task_id=self.task_id,
            kind=MemoryKind.EPISODIC,
            scope_id=self.task_id,
            content=content,
            retrieval_text=retrieval,
            source_ids=source_ids,
            importance=importance,
            confidence=1.0,
            estimated_tokens=math.ceil(len(retrieval.encode("utf-8")) / 3) + 4,
        )

    def _restore_records(self, records: list[MemoryRecord]) -> None:
        for record in sorted(records, key=lambda item: (item.created_at, item.id)):
            if record.kind is not MemoryKind.EPISODIC:
                continue
            if record.content.get("episode_schema") != EPISODE_CONTENT_SCHEMA:
                continue
            raw_reference = record.content.get("reference")
            if not isinstance(raw_reference, dict):
                continue
            reference = EpisodeReference.model_validate(raw_reference)
            raw_call_id = record.content.get("call_id")
            call_id = raw_call_id if isinstance(raw_call_id, str) else None
            if call_id is not None and call_id in self._recorded_call_ids:
                continue
            if reference.tool_name == "checkpoint":
                if reference.step_index in self._recorded_checkpoint_steps:
                    continue
                self._recorded_checkpoint_steps.append(reference.step_index)
            self._apply_reference(reference, call_id=call_id)

    def _latest_recovery(self) -> EpisodeReference | None:
        return next(
            (
                episode
                for episode in reversed(self._recent_episodes)
                if episode.recovers_episode_ids
            ),
            None,
        )


def _plan_phase(plan: Plan | None) -> str:
    if plan is None:
        return "unplanned"
    running = next((item for item in plan.items if item.status is StepStatus.RUNNING), None)
    if running is not None:
        return " ".join(running.description.split())[:300]
    pending = next((item for item in plan.items if item.status is StepStatus.PENDING), None)
    if pending is not None:
        return " ".join(pending.description.split())[:300]
    return "completed"


def _episode_paths(
    call: ToolCall,
    changed_paths: list[str],
    redactor: SecretRedactor,
) -> list[str]:
    raw_path = call.arguments.get("path")
    if isinstance(raw_path, str):
        paths = {raw_path}
    elif call.name in _VERIFICATION_TOOLS:
        paths = set(changed_paths)
    else:
        paths = set()
    return sorted(redactor.redact_text(path) for path in paths)


def _summary(output: str, redactor: SecretRedactor) -> str:
    compact = " ".join(redactor.redact_text(output).split()) or "no output"
    if len(compact) <= 320:
        return compact
    return compact[:220] + " ... " + compact[-90:]


def _hash_payload(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256(":".join(values).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"
