"""Metrics and deterministic replay derived from append-only task traces."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from patchloop.events import Event


class TaskMetrics(BaseModel):
    task_id: str
    status: str = "unknown"
    events: int = Field(default=0, ge=0)
    steps: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    failed_tool_calls: int = Field(default=0, ge=0)
    approvals_requested: int = Field(default=0, ge=0)
    approvals_denied: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    tool_duration_ms: float = Field(default=0.0, ge=0)
    elapsed_ms: float = Field(default=0.0, ge=0)
    working_memory_updates: int = Field(default=0, ge=0)
    working_memory_evictions: int = Field(default=0, ge=0)
    memory_promotions: int = Field(default=0, ge=0)
    max_working_memory_tokens_used: int = Field(default=0, ge=0)
    episodes_created: int = Field(default=0, ge=0)
    episode_recoveries: int = Field(default=0, ge=0)
    repeated_failed_actions_blocked: int = Field(default=0, ge=0)
    errors: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def from_events(cls, task_id: str, events: list[Event]) -> TaskMetrics:
        selected = [event for event in events if event.task_id == task_id]
        metrics = cls(task_id=task_id, events=len(selected))
        step_indices: set[int] = set()
        errors: Counter[str] = Counter()
        for event in selected:
            if event.type == "step.started" and isinstance(event.data.get("step"), int):
                step_indices.add(event.data["step"])
            elif event.type == "model.completed":
                metrics.model_calls += 1
                usage = event.data.get("usage", {})
                if isinstance(usage, dict):
                    metrics.input_tokens += int(usage.get("input_tokens", 0))
                    metrics.output_tokens += int(usage.get("output_tokens", 0))
                    metrics.cost_usd += float(usage.get("cost_usd", 0.0))
            elif event.type == "tool.completed":
                metrics.tool_calls += 1
                result = event.data.get("result", {})
                if isinstance(result, dict):
                    metrics.tool_duration_ms += float(result.get("duration_ms", 0.0))
                    if not result.get("success", False):
                        metrics.failed_tool_calls += 1
                        errors[str(result.get("error_kind") or "unknown")] += 1
            elif event.type == "security.decision":
                assessment = event.data.get("assessment", {})
                if isinstance(assessment, dict) and assessment.get("approval_required"):
                    metrics.approvals_requested += 1
                    if not assessment.get("allowed"):
                        metrics.approvals_denied += 1
            elif event.type == "working_memory.updated":
                metrics.working_memory_updates += 1
                metrics.working_memory_evictions = max(
                    metrics.working_memory_evictions,
                    int(event.data.get("evicted_count", 0)),
                )
                metrics.max_working_memory_tokens_used = max(
                    metrics.max_working_memory_tokens_used,
                    int(event.data.get("estimated_tokens", 0)),
                )
            elif event.type == "memory.promoted":
                metrics.memory_promotions += int(event.data.get("records", 0))
            elif event.type == "episode.created":
                metrics.episodes_created += 1
                if event.data.get("recovers_episode_ids"):
                    metrics.episode_recoveries += 1
            elif event.type == "episode.repeat_blocked":
                metrics.repeated_failed_actions_blocked += 1
            elif event.type in {"task.completed", "task.failed", "task.cancelled"}:
                metrics.status = event.type.removeprefix("task.")
                if event.type == "task.failed":
                    errors[str(event.data.get("error_kind") or "unknown")] += 1
        metrics.steps = len(step_indices)
        metrics.errors = dict(sorted(errors.items()))
        if len(selected) >= 2:
            metrics.elapsed_ms = max(
                0.0,
                (selected[-1].timestamp - selected[0].timestamp).total_seconds() * 1_000,
            )
        return metrics


class ReplayFrame(BaseModel):
    sequence: int
    timestamp: datetime
    type: str
    step: int | None = None
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class TaskReplay(BaseModel):
    task_id: str
    frames: list[ReplayFrame]

    @classmethod
    def from_events(cls, task_id: str, events: list[Event]) -> TaskReplay:
        frames = []
        current_step: int | None = None
        for event in events:
            if event.task_id != task_id:
                continue
            event_step = event.data.get("step")
            if event.type == "step.started" and isinstance(event_step, int):
                current_step = event_step
            frames.append(
                ReplayFrame(
                    sequence=event.sequence,
                    timestamp=event.timestamp,
                    type=event.type,
                    step=event_step if isinstance(event_step, int) else current_step,
                    summary=_event_summary(event),
                    data=event.data,
                )
            )
            if event.type == "step.completed":
                current_step = None
        return cls(task_id=task_id, frames=frames)


def _event_summary(event: Event) -> str:
    if event.type == "tool.completed":
        result = event.data.get("result", {})
        if isinstance(result, dict):
            state = "passed" if result.get("success") else "failed"
            return f"{result.get('tool_name', 'tool')} {state}"
    if event.type == "security.decision":
        assessment = event.data.get("assessment", {})
        if isinstance(assessment, dict):
            return f"{assessment.get('risk', 'unknown')} risk: {assessment.get('reason', '')}"
    if event.type.startswith("task."):
        return event.type.replace(".", " ")
    if event.type.startswith("step."):
        return f"step {event.data.get('step', '?')} {event.type.removeprefix('step.')}"
    return event.type.replace(".", " ")
