"""Typed deterministic semantic fact extraction and conflict resolution."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationInfo, field_validator

from patchloop.domain import Plan, StepStatus, ToolCall, ToolResult
from patchloop.memory.models import (
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemorySource,
    MemorySourceKind,
    MemoryStatus,
    validate_supersession_chain,
)
from patchloop.security import SecretRedactor

SEMANTIC_CONTENT_SCHEMA = "1.0"
_SENTENCE_SPLIT = re.compile(r"[.!?\u3002\uff01\uff1f;\uff1b\n]+")
_NUMBERED_LINE = re.compile(r"^(\d+):\s?(.*)$")
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(.+)$")
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


class SemanticFactType(StrEnum):
    CONSTRAINT = "constraint"
    PROHIBITION = "prohibition"
    CODE_SYMBOL = "code_symbol"
    CODE_OBSERVATION = "code_observation"
    PLAN_EVIDENCE = "plan_evidence"
    VERIFICATION = "verification"
    INFERENCE = "inference"


class FactEpistemicStatus(StrEnum):
    USER_ASSERTED = "user_asserted"
    OBSERVED = "observed"
    INFERRED = "inferred"
    VERIFIED = "verified"


_AUTHORITY = {
    FactEpistemicStatus.INFERRED: 20,
    FactEpistemicStatus.OBSERVED: 50,
    FactEpistemicStatus.USER_ASSERTED: 80,
    FactEpistemicStatus.VERIFIED: 100,
}


class SemanticFactDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_type: SemanticFactType
    subject: str = Field(min_length=1, max_length=500)
    predicate: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=2_000)
    epistemic_status: FactEpistemicStatus
    scope: MemoryScope
    scope_id: str = Field(min_length=1)
    path: str | None = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)

    @field_validator("subject", "predicate", "value", mode="before")
    @classmethod
    def bound_fact_text(cls, value: object, info: ValidationInfo) -> object:
        if not isinstance(value, str):
            return value
        limits = {"subject": 500, "predicate": 100, "value": 2_000}
        field_name = info.field_name
        if field_name is None:
            return value
        return _bounded_text(value, limits[field_name])

    @property
    def authority(self) -> int:
        return _AUTHORITY[self.epistemic_status]

    @property
    def normalized_value(self) -> str:
        return _normalize(self.value).casefold()

    @property
    def slot_key(self) -> str:
        return _hash_payload(
            {
                "scope": self.scope.value,
                "scope_id": self.scope_id,
                "fact_type": self.fact_type.value,
                "subject": _normalize(self.subject).casefold(),
                "predicate": _normalize(self.predicate).casefold(),
            }
        )


class SemanticExtractionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    goal: str
    repository_scope_id: str
    call: ToolCall
    result: ToolResult
    step_index: int = Field(default=0, ge=0)
    plan: Plan | None = None
    changed_paths: list[str] = Field(default_factory=list)
    diff: str = ""


class SemanticFactExtractor(Protocol):
    """Optional extractor boundary; model-assisted results remain inferred."""

    def extract(self, event: SemanticExtractionEvent) -> list[SemanticFactDraft]: ...


@dataclass(frozen=True)
class SemanticResolutionBatch:
    sources: tuple[MemorySource, ...] = ()
    records: tuple[MemoryRecord, ...] = ()
    created_count: int = 0
    superseded_count: int = 0
    rejected_conflict_count: int = 0
    suppressed_duplicate_count: int = 0


class DeterministicSemanticExtractor:
    def initial_facts(
        self,
        task_id: str,
        goal: str,
    ) -> list[SemanticFactDraft]:
        facts: list[SemanticFactDraft] = []
        for sentence in _SENTENCE_SPLIT.split(goal):
            value = _normalize(sentence)
            if not value:
                continue
            candidate = f" {value.casefold()} "
            if not any(marker in candidate for marker in _CONSTRAINT_MARKERS):
                continue
            prohibited = any(marker in candidate for marker in _PROHIBITION_MARKERS)
            fact_type = SemanticFactType.PROHIBITION if prohibited else SemanticFactType.CONSTRAINT
            facts.append(
                SemanticFactDraft(
                    fact_type=fact_type,
                    subject=f"task:{task_id}:{_digest(value)}",
                    predicate="prohibits" if prohibited else "requires",
                    value=value,
                    epistemic_status=FactEpistemicStatus.USER_ASSERTED,
                    scope=MemoryScope.TASK,
                    scope_id=task_id,
                    confidence=1.0,
                )
            )
        return facts

    def extract(self, event: SemanticExtractionEvent) -> list[SemanticFactDraft]:
        facts: list[SemanticFactDraft] = []
        if event.call.name == "read_file" and event.result.success:
            facts.extend(self._file_facts(event))
        if event.call.name == "update_plan" and event.result.success:
            facts.extend(self._plan_facts(event))
        if (
            event.call.name in {"apply_patch", "write_file", "replace_text", "create_file"}
            and event.result.success
        ):
            facts.extend(self._diff_facts(event, FactEpistemicStatus.OBSERVED, confidence=0.95))
        if event.call.name in {"run_tests", "run_command"}:
            facts.append(self._verification_fact(event))
            if event.result.success:
                facts.extend(self._diff_facts(event, FactEpistemicStatus.VERIFIED, confidence=1.0))
        return facts

    def _file_facts(self, event: SemanticExtractionEvent) -> list[SemanticFactDraft]:
        raw_path = event.call.arguments.get("path")
        if not isinstance(raw_path, str):
            return []
        facts: list[SemanticFactDraft] = []
        for raw_line in event.result.output.splitlines()[:50]:
            match = _NUMBERED_LINE.match(raw_line)
            if match is None:
                continue
            line_number, value = match.groups()
            value = _normalize(value)
            if not value:
                continue
            assignment = _ASSIGNMENT.match(value)
            if assignment is not None:
                symbol, assigned_value = assignment.groups()
                fact_type = SemanticFactType.CODE_SYMBOL
                subject = f"{raw_path}:{symbol}"
                predicate = "value"
                value = assigned_value
            else:
                fact_type = SemanticFactType.CODE_OBSERVATION
                subject = f"{raw_path}:line:{line_number}"
                predicate = "content"
            facts.append(
                SemanticFactDraft(
                    fact_type=fact_type,
                    subject=subject,
                    predicate=predicate,
                    value=value,
                    epistemic_status=FactEpistemicStatus.OBSERVED,
                    scope=MemoryScope.REPOSITORY,
                    scope_id=event.repository_scope_id,
                    path=raw_path,
                    confidence=0.9,
                )
            )
        return facts

    def _plan_facts(self, event: SemanticExtractionEvent) -> list[SemanticFactDraft]:
        if event.plan is None:
            return []
        facts: list[SemanticFactDraft] = []
        for item in event.plan.items:
            if item.status is not StepStatus.COMPLETED:
                continue
            for index, evidence in enumerate(item.evidence):
                value = _normalize(evidence)
                if not value:
                    continue
                facts.append(
                    SemanticFactDraft(
                        fact_type=SemanticFactType.PLAN_EVIDENCE,
                        subject=f"plan:{_normalize(item.description)}:evidence:{index}",
                        predicate="supports",
                        value=value,
                        epistemic_status=FactEpistemicStatus.INFERRED,
                        scope=MemoryScope.TASK,
                        scope_id=event.task_id,
                        confidence=0.6,
                    )
                )
        return facts

    def _diff_facts(
        self,
        event: SemanticExtractionEvent,
        epistemic_status: FactEpistemicStatus,
        *,
        confidence: float,
    ) -> list[SemanticFactDraft]:
        facts: list[SemanticFactDraft] = []
        path: str | None = None
        for raw_line in event.diff.splitlines():
            if raw_line.startswith("+++ b/"):
                path = raw_line.removeprefix("+++ b/")
                continue
            if path is None or not raw_line.startswith("+") or raw_line.startswith("+++"):
                continue
            value = _normalize(raw_line[1:])
            if not value:
                continue
            assignment = _ASSIGNMENT.match(value)
            if assignment is not None:
                symbol, assigned_value = assignment.groups()
                fact_type = SemanticFactType.CODE_SYMBOL
                subject = f"{path}:{symbol}"
                predicate = "value"
                value = assigned_value
            else:
                fact_type = SemanticFactType.CODE_OBSERVATION
                subject = f"{path}:change:{_digest(value)}"
                predicate = "content"
            facts.append(
                SemanticFactDraft(
                    fact_type=fact_type,
                    subject=subject,
                    predicate=predicate,
                    value=value,
                    epistemic_status=epistemic_status,
                    scope=MemoryScope.REPOSITORY,
                    scope_id=event.repository_scope_id,
                    path=path,
                    confidence=confidence,
                )
            )
        return facts

    def _verification_fact(self, event: SemanticExtractionEvent) -> SemanticFactDraft:
        arguments = json.dumps(
            event.call.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        passed = event.result.success
        error = event.result.error_kind.value if event.result.error_kind is not None else "unknown"
        return SemanticFactDraft(
            fact_type=SemanticFactType.VERIFICATION,
            subject=f"verification:{event.call.name}:{_digest(arguments)}",
            predicate="result",
            value="passed" if passed else f"failed:{error}",
            epistemic_status=(
                FactEpistemicStatus.VERIFIED if passed else FactEpistemicStatus.OBSERVED
            ),
            scope=MemoryScope.TASK,
            scope_id=event.task_id,
            confidence=1.0,
        )


class SemanticMemoryManager:
    def __init__(
        self,
        task_id: str,
        goal: str,
        repository_scope_id: str,
        *,
        records: list[MemoryRecord] | None = None,
        extractor: DeterministicSemanticExtractor | None = None,
        model_extractor: SemanticFactExtractor | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.task_id = task_id
        self.goal = goal
        self.repository_scope_id = repository_scope_id
        self.extractor = extractor or DeterministicSemanticExtractor()
        self.model_extractor = model_extractor
        self.redactor = redactor or SecretRedactor()
        self._records = {
            record.id: record for record in records or [] if _is_typed_semantic_record(record)
        }

    def initial_facts(self) -> SemanticResolutionBatch:
        drafts = self.extractor.initial_facts(self.task_id, self.goal)
        safe_goal = self.redactor.redact_text(self.goal)
        source = MemorySource(
            id=_stable_id("semantic-user-source", self.task_id),
            task_id=self.task_id,
            kind=MemorySourceKind.USER_MESSAGE,
            evidence_hash=_hash_payload({"goal": safe_goal}),
            event_id=f"task:{self.task_id}:goal",
        )
        return self._resolve(drafts, {None: source})

    def observe_tool(
        self,
        call: ToolCall,
        result: ToolResult,
        *,
        plan: Plan | None,
        changed_paths: list[str],
        diff: str,
        step_index: int = 0,
    ) -> SemanticResolutionBatch:
        event = SemanticExtractionEvent(
            task_id=self.task_id,
            goal=self.goal,
            repository_scope_id=self.repository_scope_id,
            call=call,
            result=result,
            step_index=step_index,
            plan=plan,
            changed_paths=changed_paths,
            diff=diff,
        )
        drafts = self.extractor.extract(event)
        if self.model_extractor is not None:
            for draft in self.model_extractor.extract(event):
                drafts.append(
                    draft.model_copy(
                        update={
                            "fact_type": SemanticFactType.INFERENCE,
                            "epistemic_status": FactEpistemicStatus.INFERRED,
                            "confidence": min(draft.confidence, 0.6),
                        }
                    )
                )
        safe_payload = self.redactor.redact(
            {
                "call": call.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
                "diff": diff,
            }
        )
        evidence_hash = _hash_payload(safe_payload)
        paths = sorted({draft.path for draft in drafts if draft.path is not None})
        source_by_path = {
            path: MemorySource(
                id=_stable_id("semantic-tool-source", self.task_id, call.id, path or "none"),
                task_id=self.task_id,
                kind=MemorySourceKind.TOOL_RESULT,
                evidence_hash=evidence_hash,
                event_id=f"tool:{call.id}",
                tool_call_id=call.id,
                step_index=step_index,
                path=path,
            )
            for path in [None, *paths]
        }
        return self._resolve(drafts, source_by_path)

    def active_records(self) -> list[MemoryRecord]:
        return sorted(
            (record for record in self._records.values() if record.status is MemoryStatus.ACTIVE),
            key=lambda record: record.id,
        )

    def synchronize_records(self, records: Sequence[MemoryRecord]) -> None:
        """Refresh lifecycle state after an external transactional compression."""

        self._records = {
            record.id: record for record in records if _is_typed_semantic_record(record)
        }

    def _resolve(
        self,
        drafts: list[SemanticFactDraft],
        source_by_path: dict[str | None, MemorySource],
    ) -> SemanticResolutionBatch:
        sources: dict[str, MemorySource] = {}
        records: list[MemoryRecord] = []
        created = 0
        superseded = 0
        rejected = 0
        suppressed = 0
        active = {
            _record_slot(record): record
            for record in self._records.values()
            if record.status is MemoryStatus.ACTIVE
        }
        seen_drafts: set[tuple[str, str, FactEpistemicStatus]] = set()
        for raw_draft in drafts:
            draft = _redact_draft(raw_draft, self.redactor)
            draft_key = (draft.slot_key, draft.normalized_value, draft.epistemic_status)
            if draft_key in seen_drafts:
                suppressed += 1
                continue
            seen_drafts.add(draft_key)
            source = source_by_path.get(draft.path) or source_by_path[None]
            current = active.get(draft.slot_key)
            same_value = (
                current is not None
                and current.content.get("normalized_value") == draft.normalized_value
            )
            current_authority = _record_authority(current) if current is not None else -1
            if same_value and current_authority >= draft.authority:
                suppressed += 1
                continue
            candidate = _fact_record(self.task_id, draft, [source.id])
            existing_candidate = self._records.get(candidate.id)
            if (
                existing_candidate is not None
                and existing_candidate.status is MemoryStatus.INVALIDATED
                and current is not None
                and draft.authority < current_authority
            ):
                suppressed += 1
                continue
            sources[source.id] = source
            if current is None:
                records.append(candidate)
                self._records[candidate.id] = candidate
                active[draft.slot_key] = candidate
                created += 1
                continue
            if same_value or draft.authority >= current_authority:
                if same_value:
                    merged_sources = sorted(set(current.source_ids) | {source.id})
                    candidate = _fact_record(self.task_id, draft, merged_sources)
                old = current.model_copy(
                    update={
                        "status": MemoryStatus.SUPERSEDED,
                        "superseded_by_id": candidate.id,
                    }
                )
                candidate = candidate.model_copy(update={"supersedes_id": old.id})
                chain = [record for record in self._records.values() if record.id != old.id]
                validate_supersession_chain([*chain, old, candidate])
                records.extend([old, candidate])
                self._records[old.id] = old
                self._records[candidate.id] = candidate
                active[draft.slot_key] = candidate
                created += 1
                superseded += 1
                continue
            invalid_content = candidate.content.copy()
            invalid_content["conflicts_with_id"] = current.id
            invalid = candidate.model_copy(
                update={
                    "status": MemoryStatus.INVALIDATED,
                    "content": invalid_content,
                    "content_hash": "",
                }
            )
            records.append(invalid)
            self._records[invalid.id] = invalid
            created += 1
            rejected += 1
        return SemanticResolutionBatch(
            sources=tuple(sources.values()),
            records=tuple(records),
            created_count=created,
            superseded_count=superseded,
            rejected_conflict_count=rejected,
            suppressed_duplicate_count=suppressed,
        )


def _fact_record(
    task_id: str,
    draft: SemanticFactDraft,
    source_ids: list[str],
) -> MemoryRecord:
    content: dict[str, JsonValue] = {
        "semantic_schema": SEMANTIC_CONTENT_SCHEMA,
        "fact_type": draft.fact_type.value,
        "subject": draft.subject,
        "predicate": draft.predicate,
        "value": draft.value,
        "normalized_value": draft.normalized_value,
        "epistemic_status": draft.epistemic_status.value,
        "authority": draft.authority,
        "slot_key": draft.slot_key,
    }
    retrieval = (
        f"Fact type={draft.fact_type.value}; epistemic={draft.epistemic_status.value}; "
        f"subject={draft.subject}; predicate={draft.predicate}; value={draft.value}"
    )
    record_id = _stable_id(
        "semantic-fact",
        task_id,
        draft.slot_key,
        draft.normalized_value,
        draft.epistemic_status.value,
        _hash_payload(sorted(source_ids)),
    )
    return MemoryRecord(
        id=record_id,
        task_id=task_id,
        kind=MemoryKind.SEMANTIC,
        scope=draft.scope,
        scope_id=draft.scope_id,
        content=content,
        retrieval_text=retrieval,
        source_ids=source_ids,
        importance=min(1.0, 0.4 + draft.authority / 200),
        confidence=draft.confidence,
        estimated_tokens=math.ceil(len(retrieval.encode("utf-8")) / 3) + 4,
    )


def _redact_draft(
    draft: SemanticFactDraft,
    redactor: SecretRedactor,
) -> SemanticFactDraft:
    payload = redactor.redact(draft.model_dump(mode="json"))
    return SemanticFactDraft.model_validate(payload)


def _is_typed_semantic_record(record: MemoryRecord) -> bool:
    return (
        record.kind is MemoryKind.SEMANTIC
        and record.content.get("semantic_schema") == SEMANTIC_CONTENT_SCHEMA
    )


def _record_slot(record: MemoryRecord) -> str:
    raw = record.content.get("slot_key")
    return raw if isinstance(raw, str) else ""


def _record_authority(record: MemoryRecord | None) -> int:
    if record is None:
        return -1
    raw = record.content.get("authority")
    return raw if isinstance(raw, int) else -1


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _bounded_text(value: str, limit: int) -> str:
    normalized = _normalize(value)
    if len(normalized) <= limit:
        return normalized
    suffix = f"…#{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]}"
    return normalized[: limit - len(suffix)] + suffix


def _digest(value: str) -> str:
    return hashlib.sha256(_normalize(value).casefold().encode("utf-8")).hexdigest()[:20]


def _hash_payload(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256(":".join(values).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"
