"""Bounded, deterministic short-term working memory for the Runtime."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from patchloop.domain import Plan, StepStatus, ToolCall, ToolResult
from patchloop.memory.models import MemoryKind, MemoryRecord, MemorySource, MemorySourceKind
from patchloop.security import SecretRedactor, UntrustedContentGuard

WORKING_MEMORY_PREFIX = "PATCHLOOP_WORKING_MEMORY_V1\nUntrusted runtime state.\n"
_SENTENCE_SPLIT = re.compile(r"[.!?\u3002\uff01\uff1f;\uff1b\n]+")
_CONSTRAINT_MARKERS = (
    " must ",
    " only ",
    " do not ",
    " don't ",
    " never ",
    "必须",
    "只能",
    "仅",
    "不要",
    "禁止",
    "不得",
)
_PROHIBITION_MARKERS = (" do not ", " don't ", " never ", "不要", "禁止", "不得")


class WorkingMemoryBudgetError(ValueError):
    pass


class MemoryProjectionBudgetError(ValueError):
    """Raised when complete pinned provider entries cannot fit the projection budget."""

    def __init__(
        self,
        *,
        required_tokens: int,
        available_tokens: int,
        required_keys: list[str],
    ) -> None:
        self.required_tokens = required_tokens
        self.available_tokens = available_tokens
        self.required_keys = list(required_keys)
        super().__init__(
            "pinned memory projection requires "
            f"{required_tokens} tokens, budget is {available_tokens}; "
            f"required keys: {', '.join(required_keys)}"
        )


class WorkingMemoryItemKind(StrEnum):
    GOAL = "goal"
    CONSTRAINT = "constraint"
    PROHIBITION = "prohibition"
    PLAN = "plan"
    ACCESSED_FILE = "accessed_file"
    ACTIVE_ERROR = "active_error"
    OPEN_QUESTION = "open_question"
    CHANGED_FILE = "changed_file"
    KEY_EVIDENCE = "key_evidence"
    RECENT_RESULT = "recent_result"


class WorkingMemoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, max_length=160)
    kind: WorkingMemoryItemKind
    text: str = Field(min_length=1, max_length=1_200)
    pinned: bool = False
    step_index: int = Field(default=0, ge=0)
    source_id: str | None = None


class WorkingMemoryProviderEntry(BaseModel):
    """One stable, complete working-memory value exposed to the provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, max_length=160)
    kind: WorkingMemoryItemKind
    value: str = Field(min_length=1, max_length=1_200)
    pinned: bool = False


_PROVIDER_FIELDS = {
    WorkingMemoryItemKind.CONSTRAINT: "constraints",
    WorkingMemoryItemKind.PROHIBITION: "prohibitions",
    WorkingMemoryItemKind.PLAN: "plan",
    WorkingMemoryItemKind.ACCESSED_FILE: "read_files",
    WorkingMemoryItemKind.ACTIVE_ERROR: "active_errors",
    WorkingMemoryItemKind.OPEN_QUESTION: "questions",
    WorkingMemoryItemKind.CHANGED_FILE: "changed_files",
    WorkingMemoryItemKind.KEY_EVIDENCE: "key_evidence",
    WorkingMemoryItemKind.RECENT_RESULT: "recent_results",
}


def working_memory_provider_field(kind: WorkingMemoryItemKind) -> str:
    """Return the stable V2 field name for a provider-visible working item."""

    try:
        return _PROVIDER_FIELDS[kind]
    except KeyError as exc:
        raise ValueError(f"working memory kind is not provider-visible: {kind.value}") from exc


def project_working_entries(
    snapshot: WorkingMemorySnapshot,
    *,
    redactor: SecretRedactor | None = None,
) -> list[WorkingMemoryProviderEntry]:
    """Project safe provider entries without rendering or truncating nested JSON."""

    active_redactor = redactor or SecretRedactor()
    guard = UntrustedContentGuard(active_redactor)
    recent_keys = {
        item.key
        for item in sorted(
            (item for item in snapshot.items if item.kind is WorkingMemoryItemKind.RECENT_RESULT),
            key=lambda item: (-item.step_index, item.key),
        )[:2]
    }
    entries: list[WorkingMemoryProviderEntry] = []
    for item in snapshot.items:
        if item.kind is WorkingMemoryItemKind.GOAL:
            continue
        if item.kind is WorkingMemoryItemKind.RECENT_RESULT and item.key not in recent_keys:
            continue
        safe_value = guard.inspect(active_redactor.redact_text(item.text)).safe_text
        if not safe_value:
            continue
        entries.append(
            WorkingMemoryProviderEntry(
                key=item.key,
                kind=item.kind,
                value=safe_value,
                pinned=item.pinned,
            )
        )
    return sorted(entries, key=lambda entry: entry.key)


class WorkingMemoryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str = Field(min_length=1)
    step_index: int = Field(ge=0)
    tool_name: str = Field(min_length=1)
    success: bool
    summary: str = Field(min_length=1, max_length=400)
    changed_paths: list[str] = Field(default_factory=list)


class WorkingMemorySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=1)
    token_budget: int = Field(ge=128)
    revision: int = Field(default=0, ge=0)
    items: list[WorkingMemoryItem] = Field(default_factory=list)
    phase_events: list[WorkingMemoryEvent] = Field(default_factory=list)
    read_signatures: dict[str, str] = Field(default_factory=dict, max_length=512)
    promoted_keys: list[str] = Field(default_factory=list, max_length=256)
    estimated_tokens: int = Field(default=0, ge=0)
    max_estimated_tokens: int = Field(default=0, ge=0)
    evicted_count: int = Field(default=0, ge=0)
    promoted_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        keys = [item.key for item in self.items]
        calls = [event.call_id for event in self.phase_events]
        if len(keys) != len(set(keys)):
            raise ValueError("working memory item keys must be unique")
        if len(calls) != len(set(calls)):
            raise ValueError("working memory phase event call ids must be unique")
        if len(self.promoted_keys) != len(set(self.promoted_keys)):
            raise ValueError("working memory promoted keys must be unique")
        if self.estimated_tokens > self.token_budget:
            raise ValueError("working memory snapshot exceeds token budget")
        if self.max_estimated_tokens < self.estimated_tokens:
            raise ValueError("working memory maximum cannot be below current usage")
        return self


@dataclass(frozen=True)
class MemoryPromotionBatch:
    sources: tuple[MemorySource, ...] = ()
    records: tuple[MemoryRecord, ...] = ()


class WorkingMemoryManager:
    def __init__(
        self,
        task_id: str,
        goal: str,
        *,
        token_budget: int,
        snapshot: WorkingMemorySnapshot | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        if token_budget < 128:
            raise ValueError("working memory token budget must be at least 128")
        self.task_id = task_id
        self.token_budget = token_budget
        self.redactor = redactor or SecretRedactor()
        self._goal_terms = {
            term.casefold()
            for term in re.findall(r"[\w-]+", goal, flags=re.UNICODE)
            if len(term) >= 4
        }
        self._pending_sources: dict[str, MemorySource] = {}
        self._pending_records: dict[str, MemoryRecord] = {}
        self._items: dict[str, WorkingMemoryItem]
        self._phase_events: list[WorkingMemoryEvent]
        self._read_signatures: dict[str, str]
        self._promoted_keys: list[str]
        if snapshot is not None:
            if snapshot.task_id != task_id:
                raise ValueError("working memory snapshot belongs to another task")
            if snapshot.token_budget != token_budget:
                raise ValueError("working memory snapshot budget does not match task budget")
            self._items = {item.key: item for item in snapshot.items}
            self._phase_events = list(snapshot.phase_events)
            self._read_signatures = dict(snapshot.read_signatures)
            self._promoted_keys = list(snapshot.promoted_keys)
            self._revision = snapshot.revision
            self._evicted_count = snapshot.evicted_count
            self._promoted_count = snapshot.promoted_count
            self._max_estimated_tokens = snapshot.max_estimated_tokens
            self._rebalance()
            return
        self._items = {}
        self._phase_events = []
        self._read_signatures = {}
        self._promoted_keys = []
        self._revision = 0
        self._evicted_count = 0
        self._promoted_count = 0
        self._max_estimated_tokens = 0
        self._initialize_goal(goal)

    def pin_constraint(self, text: str, *, step_index: int = 0) -> None:
        self._upsert(
            f"constraint:{_digest(text)}",
            WorkingMemoryItemKind.CONSTRAINT,
            text,
            pinned=True,
            step_index=step_index,
        )
        self._changed()

    def pin_prohibition(self, text: str, *, step_index: int = 0) -> None:
        self._upsert(
            f"prohibition:{_digest(text)}",
            WorkingMemoryItemKind.PROHIBITION,
            text,
            pinned=True,
            step_index=step_index,
        )
        self._changed()

    def add_open_question(self, key: str, text: str, *, step_index: int) -> None:
        self._upsert(
            f"question:{key}",
            WorkingMemoryItemKind.OPEN_QUESTION,
            text,
            pinned=False,
            step_index=step_index,
        )
        self._changed()

    def resolve_open_question(self, key: str) -> None:
        if self._items.pop(f"question:{key}", None) is not None:
            self._changed()

    def has_read_call(self, call: ToolCall) -> bool:
        return call.name == "read_file" and _call_signature(call) in self._read_signatures

    def sync_plan(
        self,
        plan: Plan | None,
        *,
        step_index: int,
        source: MemorySource | None = None,
    ) -> None:
        for key in [item_key for item_key in self._items if item_key.startswith("plan:")]:
            del self._items[key]
        if plan is not None:
            for item in plan.items:
                normalized = " ".join(item.description.split())
                promotion_key = f"plan-completed:{_digest(normalized)}"
                if item.status is StepStatus.COMPLETED:
                    if source is not None and promotion_key not in self._promoted_keys:
                        self._promote_completed_plan_item(
                            normalized,
                            item.evidence,
                            source,
                            promotion_key,
                        )
                    continue
                self._upsert(
                    f"plan:{_digest(normalized)}",
                    WorkingMemoryItemKind.PLAN,
                    f"{item.status.value}: {normalized}",
                    pinned=item.status is StepStatus.RUNNING,
                    step_index=step_index,
                    source_id=source.id if source is not None else None,
                )
        self._changed()

    def sync_changed_paths(self, paths: list[str], *, step_index: int) -> None:
        active = {f"changed:{path}" for path in paths}
        for key in [item_key for item_key in self._items if item_key.startswith("changed:")]:
            if key not in active:
                del self._items[key]
        for path in sorted(set(paths)):
            self._upsert(
                f"changed:{path}",
                WorkingMemoryItemKind.CHANGED_FILE,
                path,
                pinned=False,
                step_index=step_index,
            )
        self._changed()

    def observe_tool(
        self,
        call: ToolCall,
        result: ToolResult,
        *,
        step_index: int,
        plan: Plan | None,
        changed_paths: list[str],
    ) -> MemoryPromotionBatch:
        summary = self._tool_summary(result.output)
        source = self._tool_source(call, result, step_index)
        raw_path = call.arguments.get("path")
        if (
            call.name in {"apply_patch", "replace_text", "write_file"}
            and result.success
            and isinstance(raw_path, str)
        ):
            safe_path = self.redactor.redact_text(raw_path)
            self._read_signatures = {
                signature: path
                for signature, path in self._read_signatures.items()
                if path != safe_path
            }
            self._items.pop(f"read:{_digest(safe_path)}", None)
        if all(event.call_id != call.id for event in self._phase_events):
            self._phase_events.append(
                WorkingMemoryEvent(
                    call_id=call.id,
                    step_index=step_index,
                    tool_name=result.tool_name,
                    success=result.success,
                    summary=summary,
                    changed_paths=sorted(set(changed_paths)),
                )
            )
            if self.token_budget < 256 and len(self._phase_events) > 1:
                self._phase_events = self._phase_events[-1:]
                self._evicted_count += 1
        self._upsert(
            f"result:{result.tool_name}",
            WorkingMemoryItemKind.RECENT_RESULT,
            f"{result.tool_name} {'passed' if result.success else 'failed'}: {summary}",
            pinned=False,
            step_index=step_index,
            source_id=source.id,
        )
        if call.name == "read_file" and result.success and isinstance(raw_path, str):
            safe_path = self.redactor.redact_text(raw_path)
            self._read_signatures[_call_signature(call)] = safe_path
            self._read_signatures = dict(list(self._read_signatures.items())[-512:])
            self._upsert(
                f"read:{_digest(safe_path)}",
                WorkingMemoryItemKind.ACCESSED_FILE,
                safe_path,
                pinned=False,
                step_index=step_index,
                source_id=source.id,
            )
        matching_terms = sorted(
            (term for term in self._goal_terms if term in summary.casefold()),
            key=lambda term: (-len(term), term),
        )
        if result.success and matching_terms:
            term = matching_terms[0]
            self._upsert(
                f"evidence:{term}",
                WorkingMemoryItemKind.KEY_EVIDENCE,
                self._evidence_snippet(summary, term),
                pinned=False,
                step_index=step_index,
                source_id=source.id,
            )
        error_key = f"error:{result.tool_name}"
        if result.success:
            self._items.pop(error_key, None)
        else:
            error_kind = result.error_kind.value if result.error_kind is not None else "unknown"
            self._upsert(
                error_key,
                WorkingMemoryItemKind.ACTIVE_ERROR,
                f"{result.tool_name} [{error_kind}]: {summary}",
                pinned=True,
                step_index=step_index,
                source_id=source.id,
            )
        self.sync_changed_paths(changed_paths, step_index=step_index)
        if call.name == "update_plan" and result.success:
            self.sync_plan(plan, step_index=step_index, source=source)
        if call.name in {"run_tests", "run_command"} and result.success:
            self._promote_verification(source, result, summary, step_index)
        self._changed()
        return self.drain_promotions()

    def drain_promotions(self) -> MemoryPromotionBatch:
        batch = MemoryPromotionBatch(
            sources=tuple(self._pending_sources.values()),
            records=tuple(self._pending_records.values()),
        )
        self._pending_sources.clear()
        self._pending_records.clear()
        return batch

    def snapshot(self) -> WorkingMemorySnapshot:
        estimated = self._estimate_current()
        return WorkingMemorySnapshot(
            task_id=self.task_id,
            token_budget=self.token_budget,
            revision=self._revision,
            items=self._sorted_items(),
            phase_events=list(self._phase_events),
            read_signatures=dict(self._read_signatures),
            promoted_keys=list(self._promoted_keys),
            estimated_tokens=estimated,
            max_estimated_tokens=max(self._max_estimated_tokens, estimated),
            evicted_count=self._evicted_count,
            promoted_count=self._promoted_count,
        )

    def render(self) -> str:
        return WORKING_MEMORY_PREFIX + json.dumps(
            self._context_payload(), ensure_ascii=False, separators=(",", ":")
        )

    def _initialize_goal(self, goal: str) -> None:
        safe_goal = self.redactor.redact_text(" ".join(goal.split()))
        self._upsert(
            "goal",
            WorkingMemoryItemKind.GOAL,
            safe_goal,
            pinned=True,
            step_index=0,
        )
        for sentence in _SENTENCE_SPLIT.split(safe_goal):
            normalized = " ".join(sentence.split())
            if not normalized:
                continue
            candidate = f" {normalized.casefold()} "
            if not any(marker in candidate for marker in _CONSTRAINT_MARKERS):
                continue
            kind = (
                WorkingMemoryItemKind.PROHIBITION
                if any(marker in candidate for marker in _PROHIBITION_MARKERS)
                else WorkingMemoryItemKind.CONSTRAINT
            )
            self._upsert(
                f"{kind.value}:{_digest(normalized)}",
                kind,
                normalized,
                pinned=True,
                step_index=0,
            )
        self._changed()

    def _promote_completed_plan_item(
        self,
        description: str,
        evidence: list[str],
        source: MemorySource,
        promotion_key: str,
    ) -> None:
        actions = [
            f"step {event.step_index} {event.tool_name} "
            f"{'passed' if event.success else 'failed'}: {event.summary}"
            for event in self._phase_events
        ]
        semantic_content: dict[str, JsonValue] = {
            "fact": description,
            "evidence": cast(list[JsonValue], list(evidence)),
        }
        semantic = MemoryRecord(
            id=_stable_id("wm-semantic", self.task_id, promotion_key),
            task_id=self.task_id,
            kind=MemoryKind.SEMANTIC,
            scope_id=self.task_id,
            content=semantic_content,
            retrieval_text=f"Completed plan item: {description}",
            source_ids=[source.id],
            importance=0.8,
            confidence=1.0 if evidence else 0.8,
            estimated_tokens=_estimate_text(description + " ".join(evidence)),
        )
        episodic_content: dict[str, JsonValue] = {
            "intent": description,
            "outcome": "completed",
            "actions": cast(list[JsonValue], actions),
            "evidence": cast(list[JsonValue], list(evidence)),
        }
        episodic = MemoryRecord(
            id=_stable_id("wm-episode", self.task_id, promotion_key),
            task_id=self.task_id,
            kind=MemoryKind.EPISODIC,
            scope_id=self.task_id,
            content=episodic_content,
            retrieval_text=f"Completed phase {description}; actions: {'; '.join(actions)}",
            source_ids=[source.id],
            importance=0.7,
            confidence=0.9,
            estimated_tokens=_estimate_text(description + " ".join(actions + evidence)),
        )
        self._queue_promotion(promotion_key, source, [semantic, episodic])
        self._phase_events.clear()

    def _promote_verification(
        self,
        source: MemorySource,
        result: ToolResult,
        summary: str,
        step_index: int,
    ) -> None:
        promotion_key = f"verification:{result.call_id}"
        if promotion_key in self._promoted_keys:
            return
        record = MemoryRecord(
            id=_stable_id("wm-verification", self.task_id, result.call_id),
            task_id=self.task_id,
            kind=MemoryKind.SEMANTIC,
            scope_id=self.task_id,
            content={
                "verification_tool": result.tool_name,
                "result": summary,
                "step_index": step_index,
            },
            retrieval_text=f"Verified by {result.tool_name}: {summary}",
            source_ids=[source.id],
            importance=0.9,
            confidence=1.0,
            estimated_tokens=_estimate_text(summary),
        )
        self._queue_promotion(promotion_key, source, [record])

    def _queue_promotion(
        self,
        key: str,
        source: MemorySource,
        records: list[MemoryRecord],
    ) -> None:
        self._pending_sources[source.id] = source
        for record in records:
            self._pending_records[record.id] = record
        self._promoted_keys = [
            *[item for item in self._promoted_keys if item != key],
            key,
        ][-256:]
        self._promoted_count += len(records)

    def _tool_source(self, call: ToolCall, result: ToolResult, step_index: int) -> MemorySource:
        safe_payload = self.redactor.redact(result.model_dump(mode="json"))
        evidence_hash = hashlib.sha256(
            json.dumps(safe_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        raw_path = call.arguments.get("path")
        path = self.redactor.redact_text(raw_path) if isinstance(raw_path, str) else None
        return MemorySource(
            id=_stable_id("wm-source", self.task_id, call.id),
            task_id=self.task_id,
            kind=MemorySourceKind.TOOL_RESULT,
            evidence_hash=evidence_hash,
            event_id=f"tool:{call.id}",
            step_index=step_index,
            tool_call_id=call.id,
            path=path,
        )

    def _tool_summary(self, output: str) -> str:
        safe = self.redactor.redact_text(output)
        try:
            payload = json.loads(safe)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("output"), str):
            safe = payload["output"]
        compact = " ".join(safe.split()) or "no output"
        if len(compact) <= 320:
            return compact
        return compact[:220] + " ... " + compact[-90:]

    @staticmethod
    def _evidence_snippet(summary: str, term: str) -> str:
        match_at = summary.casefold().find(term)
        if match_at < 0 or len(summary) <= 96:
            return summary[:96]
        start = max(0, match_at - 16)
        end = min(len(summary), match_at + len(term) + 64)
        prefix = "... " if start else ""
        suffix = " ..." if end < len(summary) else ""
        return prefix + summary[start:end] + suffix

    def _upsert(
        self,
        key: str,
        kind: WorkingMemoryItemKind,
        text: str,
        *,
        pinned: bool,
        step_index: int,
        source_id: str | None = None,
    ) -> None:
        safe_text = self.redactor.redact_text(" ".join(text.split()))
        self._items[key] = WorkingMemoryItem(
            key=key,
            kind=kind,
            text=safe_text[:1_200],
            pinned=pinned or kind is WorkingMemoryItemKind.ACTIVE_ERROR,
            step_index=step_index,
            source_id=source_id,
        )

    def _changed(self) -> None:
        self._revision += 1
        self._rebalance()

    def _rebalance(self) -> None:
        while self._estimate_current() > self.token_budget:
            choices: list[tuple[int, int, str, str]] = []
            ranks = {
                WorkingMemoryItemKind.RECENT_RESULT: 0,
                WorkingMemoryItemKind.CHANGED_FILE: 2,
                WorkingMemoryItemKind.OPEN_QUESTION: 3,
                WorkingMemoryItemKind.PLAN: 4,
                WorkingMemoryItemKind.KEY_EVIDENCE: 5,
                WorkingMemoryItemKind.ACCESSED_FILE: 6,
            }
            for item in self._items.values():
                if item.pinned or item.kind is WorkingMemoryItemKind.ACTIVE_ERROR:
                    continue
                choices.append((ranks.get(item.kind, 5), item.step_index, item.key, "item"))
            for event in self._phase_events:
                choices.append((1, event.step_index, event.call_id, "event"))
            if not choices:
                mandatory = self._estimate_current()
                raise WorkingMemoryBudgetError(
                    f"mandatory working memory requires {mandatory} tokens, "
                    f"budget is {self.token_budget}"
                )
            _, _, identifier, choice_type = min(choices)
            if choice_type == "item":
                del self._items[identifier]
            else:
                self._phase_events = [
                    event for event in self._phase_events if event.call_id != identifier
                ]
            self._evicted_count += 1
        current = self._estimate_current()
        self._max_estimated_tokens = max(self._max_estimated_tokens, current)

    def _estimate_current(self) -> int:
        serialized = self.render().encode("utf-8")
        return math.ceil(len(serialized) / 3) + 4

    def _context_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "revision": self._revision,
            "goal": "user_message",
        }
        grouped: dict[WorkingMemoryItemKind, list[str]] = {}
        for item in self._sorted_items():
            grouped.setdefault(item.kind, []).append(item.text)
        labels = {
            WorkingMemoryItemKind.CONSTRAINT: "constraints",
            WorkingMemoryItemKind.PROHIBITION: "prohibitions",
            WorkingMemoryItemKind.PLAN: "plan",
            WorkingMemoryItemKind.ACCESSED_FILE: "read_files",
            WorkingMemoryItemKind.ACTIVE_ERROR: "active_errors",
            WorkingMemoryItemKind.OPEN_QUESTION: "questions",
            WorkingMemoryItemKind.CHANGED_FILE: "changed_files",
            WorkingMemoryItemKind.KEY_EVIDENCE: "key_evidence",
            WorkingMemoryItemKind.RECENT_RESULT: "recent_results",
        }
        for kind, label in labels.items():
            if self.token_budget < 256 and kind in {
                WorkingMemoryItemKind.KEY_EVIDENCE,
                WorkingMemoryItemKind.RECENT_RESULT,
            }:
                if kind is WorkingMemoryItemKind.KEY_EVIDENCE:
                    payload["key_evidence"] = "task_memory"
                continue
            values = grouped.get(kind)
            if values:
                payload[label] = values
        if self._phase_events and self.token_budget >= 256:
            payload["recent_events"] = [
                [event.step_index, event.tool_name, event.success, event.summary]
                for event in self._phase_events
            ]
        return payload

    def _sorted_items(self) -> list[WorkingMemoryItem]:
        return sorted(
            self._items.values(),
            key=lambda item: (not item.pinned, item.kind.value, item.key),
        )


def _estimate_text(value: str) -> int:
    return math.ceil(len(value.encode("utf-8")) / 3) + 4


def _digest(value: str) -> str:
    return hashlib.sha256(" ".join(value.casefold().split()).encode("utf-8")).hexdigest()[:20]


def _call_signature(call: ToolCall) -> str:
    payload = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256(":".join(values).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"
