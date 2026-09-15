"""Safe, provider-neutral prompt cache layout diagnostics.

Legacy JSON metrics operate on redacted data. Message metrics hash the exact
normalized messages and only retain irreversible SHA-256 fingerprints. They
describe why a request differs from the previous request; they do not implement a response cache and
must never be used as a correctness dependency.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.context.engine import ContextEngine
from patchloop.providers.base import ModelMessage, ModelUsage, ToolSpec
from patchloop.providers.continuation import ContinuationCodec
from patchloop.security import SecretRedactor


class CacheLayoutReason(StrEnum):
    COLD_START = "cold_start"
    MODEL_OR_THINKING_CHANGE = "model_or_thinking_change"
    DYNAMIC_SYSTEM_PREFIX = "dynamic_system_prefix"
    PROJECT_SNAPSHOT_CHANGE = "project_snapshot_change"
    HISTORY_RESELECTION = "history_reselection"
    TOOL_SCHEMA_CHANGE = "tool_schema_change"
    MEMORY_PROJECTION_CHANGE = "memory_projection_change"
    EPOCH_ROLLOVER = "epoch_rollover"
    PROVIDER_BEST_EFFORT = "provider_best_effort"
    UNKNOWN = "unknown"


_SECTION_ORDER = (
    "system_instructions",
    "task_project_snapshot",
    "tool_schema",
    "epoch_snapshot",
    "memory_projection",
    "history_groups",
    "request",
)
_STABLE_PREFIX_SECTIONS = _SECTION_ORDER[:4]
_REASON_PRIORITY = (
    CacheLayoutReason.COLD_START,
    CacheLayoutReason.MODEL_OR_THINKING_CHANGE,
    CacheLayoutReason.EPOCH_ROLLOVER,
    CacheLayoutReason.DYNAMIC_SYSTEM_PREFIX,
    CacheLayoutReason.PROJECT_SNAPSHOT_CHANGE,
    CacheLayoutReason.HISTORY_RESELECTION,
    CacheLayoutReason.TOOL_SCHEMA_CHANGE,
    CacheLayoutReason.MEMORY_PROJECTION_CHANGE,
    CacheLayoutReason.PROVIDER_BEST_EFFORT,
    CacheLayoutReason.UNKNOWN,
)


class CacheSectionFingerprint(BaseModel):
    """A redacted section's irreversible identity and deterministic byte size."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)


class CacheRequestFingerprint(BaseModel):
    """Fingerprints for the sections that make up one provider request.

    ``history_group_fingerprints`` contains only hashes.  It lets diagnostics
    distinguish append-only history from history re-selection without retaining
    message text in a checkpoint or trace.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    system_instructions: CacheSectionFingerprint
    task_project_snapshot: CacheSectionFingerprint
    tool_schema: CacheSectionFingerprint
    epoch_snapshot: CacheSectionFingerprint
    memory_projection: CacheSectionFingerprint
    history_groups: CacheSectionFingerprint
    request: CacheSectionFingerprint
    dynamic_system_prefix: CacheSectionFingerprint
    history_group_fingerprints: tuple[str, ...] = ()
    provider_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    epoch_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_wire_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    message_fingerprints: tuple[str, ...] | None = None
    message_estimated_tokens: tuple[int, ...] | None = None
    ordered_tools_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    binding_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_message_vector(self) -> Self:
        if self.message_fingerprints is not None:
            if self.message_estimated_tokens is None or len(self.message_fingerprints) != len(
                self.message_estimated_tokens
            ):
                raise ValueError("message fingerprint and token vectors must have equal lengths")
            if any(
                len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
                for value in self.message_fingerprints
            ):
                raise ValueError("message fingerprints must be SHA-256 digests")
            if any(value < 0 for value in self.message_estimated_tokens):
                raise ValueError("message token estimates cannot be negative")
        return self

    @property
    def section_fingerprints(self) -> dict[str, str]:
        return {
            name: getattr(self, name).fingerprint for name in _SECTION_ORDER if name != "request"
        } | {
            "dynamic_system_prefix": self.dynamic_system_prefix.fingerprint,
            "request": self.request.fingerprint,
        }

    @property
    def sections(self) -> dict[str, CacheSectionFingerprint]:
        return {name: getattr(self, name) for name in _SECTION_ORDER}

    @property
    def stable_prefix_bytes(self) -> int:
        return sum(getattr(self, name).byte_length for name in _STABLE_PREFIX_SECTIONS)

    @property
    def stable_prefix_tokens(self) -> int:
        return sum(getattr(self, name).estimated_tokens for name in _STABLE_PREFIX_SECTIONS)

    @property
    def system_fingerprint(self) -> str:
        return self.system_instructions.fingerprint

    @property
    def project_fingerprint(self) -> str:
        return self.task_project_snapshot.fingerprint

    @property
    def tool_schema_fingerprint(self) -> str:
        return self.tool_schema.fingerprint

    @property
    def memory_projection_fingerprint(self) -> str:
        return self.memory_projection.fingerprint

    @property
    def history_fingerprint(self) -> str:
        return self.history_groups.fingerprint


class CacheLayoutTrace(BaseModel):
    """One safe cache-layout observation for one model step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step: int = Field(ge=0)
    provider: str = Field(min_length=1)
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    epoch_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    section_fingerprints: dict[str, str] = Field(default_factory=dict)
    section_byte_lengths: dict[str, int] = Field(default_factory=dict)
    stable_prefix_bytes: int = Field(default=0, ge=0)
    stable_prefix_tokens: int = Field(default=0, ge=0)
    longest_common_prefix_bytes: int = Field(default=0, ge=0)
    longest_common_prefix_tokens: int = Field(default=0, ge=0)
    first_change_section: str | None = None
    reasons: list[CacheLayoutReason] = Field(default_factory=list)
    primary_reason: CacheLayoutReason = CacheLayoutReason.UNKNOWN
    secondary_reasons: list[CacheLayoutReason] = Field(default_factory=list)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    cache_usage_consistent: bool | None = None
    provider_best_effort: bool = False
    request_id: str | None = None
    source_request_id: str | None = None
    message_count: int | None = Field(default=None, ge=0)
    previous_message_count: int | None = Field(default=None, ge=0)
    common_prefix_message_count: int | None = Field(default=None, ge=0)
    previous_request_is_prefix: bool | None = None
    first_changed_message_index: int | None = Field(default=None, ge=0)
    common_prefix_estimated_tokens: int | None = Field(default=None, ge=0)
    tools_unchanged: bool | None = None
    binding_unchanged: bool | None = None
    comparison_kind: Literal["cold_start", "ordinary", "compression", "epoch_boundary"] | None = (
        None
    )
    prefix_break_reason: (
        Literal[
            "memory_insert",
            "history_rewrite",
            "tools_change",
            "binding_change",
            "security_rewrite",
            "unknown",
        ]
        | None
    ) = None
    metric_basis: Literal["normalized_messages_v1"] | None = None
    legacy_lcp_basis: Literal["canonical_json_estimate", "section_estimate"] | None = None

    @property
    def reason(self) -> CacheLayoutReason:
        """Compatibility shorthand for consumers interested in the main cause."""

        return self.primary_reason

    def with_usage(self, usage: ModelUsage, *, miss_threshold_tokens: int) -> CacheLayoutTrace:
        hit = usage.cache_hit_tokens
        miss = usage.cache_miss_tokens
        consistent = None
        if hit is not None and miss is not None:
            consistent = hit + miss == usage.input_tokens
        high_miss = miss is not None and miss > miss_threshold_tokens
        reasons = list(self.reasons)
        provider_best_effort = high_miss and not reasons
        if provider_best_effort:
            reasons.append(CacheLayoutReason.PROVIDER_BEST_EFFORT)
        if not reasons:
            reasons.append(CacheLayoutReason.UNKNOWN)
        reasons = _ordered_unique(reasons)
        primary = _primary_reason(reasons)
        return self.model_copy(
            update={
                "reasons": reasons,
                "primary_reason": primary,
                "secondary_reasons": [reason for reason in reasons if reason != primary],
                "cache_hit_tokens": hit,
                "cache_miss_tokens": miss,
                "cache_usage_consistent": consistent,
                "provider_best_effort": provider_best_effort,
            }
        )


class CacheDiagnosticsSnapshot(BaseModel):
    """Safe cross-checkpoint state required to continue layout comparisons."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    previous_request: CacheRequestFingerprint | None = None
    previous_wire_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    previous_section_lengths: dict[str, int] = Field(default_factory=dict)
    previous_section_fingerprints: dict[str, str] = Field(default_factory=dict)
    previous_provider_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    previous_epoch_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    previous_ordinary_request: CacheRequestFingerprint | None = None
    previous_ordinary_trace: CacheLayoutTrace | None = None


class CacheDiagnostics:
    """Build deterministic request fingerprints and explain cache misses."""

    def __init__(
        self,
        *,
        redactor: SecretRedactor | None = None,
        miss_threshold_tokens: int = 70_000,
    ) -> None:
        if miss_threshold_tokens < 1:
            raise ValueError("cache miss threshold must be positive")
        self.redactor = redactor or SecretRedactor()
        self.miss_threshold_tokens = miss_threshold_tokens
        self._previous: CacheRequestFingerprint | None = None
        self._previous_wire: bytes | None = None
        self._previous_ordinary: CacheRequestFingerprint | None = None
        self._previous_ordinary_trace: CacheLayoutTrace | None = None

    def reset(self) -> None:
        self._previous = None
        self._previous_wire = None
        self._previous_ordinary = None
        self._previous_ordinary_trace = None

    def snapshot(self) -> CacheDiagnosticsSnapshot:
        previous = self._previous
        return CacheDiagnosticsSnapshot(
            previous_request=previous,
            previous_wire_fingerprint=(previous.request_wire_fingerprint if previous else None),
            previous_section_lengths=(
                {name: getattr(previous, name).byte_length for name in _SECTION_ORDER}
                if previous
                else {}
            ),
            previous_section_fingerprints=previous.section_fingerprints if previous else {},
            previous_provider_fingerprint=previous.provider_fingerprint if previous else None,
            previous_epoch_fingerprint=previous.epoch_fingerprint if previous else None,
            previous_ordinary_request=self._previous_ordinary,
            previous_ordinary_trace=self._previous_ordinary_trace,
        )

    def restore(self, snapshot: CacheDiagnosticsSnapshot | None) -> None:
        self.reset()
        if snapshot is None:
            return
        self._previous = snapshot.previous_request
        self._previous_ordinary = snapshot.previous_ordinary_request or snapshot.previous_request
        self._previous_ordinary_trace = snapshot.previous_ordinary_trace

    def observe(
        self,
        step: int,
        messages: Sequence[ModelMessage],
        tools: Sequence[ToolSpec],
        *,
        provider: str,
        model: str | None = None,
        thinking: object | None = None,
        epoch_snapshot: object | None = None,
        system_instructions: str | None = None,
        task_project_snapshot: object | None = None,
        memory_projection: object | None = None,
        request_kind: Literal["ordinary", "compression"] = "ordinary",
        request_id: str | None = None,
        binding_fingerprint: str | None = None,
    ) -> CacheLayoutTrace:
        if step < 0:
            raise ValueError("cache diagnostic step must be non-negative")
        fingerprint, wire = fingerprint_request(
            messages,
            tools,
            redactor=self.redactor,
            model=model,
            thinking=thinking,
            epoch_snapshot=epoch_snapshot,
            system_instructions=system_instructions,
            task_project_snapshot=task_project_snapshot,
            memory_projection=memory_projection,
            binding_fingerprint=binding_fingerprint,
            provider=provider,
        )
        if (
            request_kind == "ordinary"
            and request_id is not None
            and self._previous_ordinary_trace is not None
            and self._previous_ordinary_trace.request_id == request_id
            and self._previous_ordinary_trace.request_fingerprint
            == fingerprint.request_wire_fingerprint
            and self._previous_ordinary is not None
            and self._previous_ordinary.message_fingerprints == fingerprint.message_fingerprints
            and self._previous_ordinary.ordered_tools_fingerprint
            == fingerprint.ordered_tools_fingerprint
            and self._previous_ordinary.binding_fingerprint == fingerprint.binding_fingerprint
        ):
            return self._previous_ordinary_trace
        previous = self._previous
        reasons, first_change = self._structural_reasons(previous, fingerprint)
        if previous is None:
            lcp = 0
        elif self._previous_wire is not None:
            lcp = _longest_common_prefix(self._previous_wire, wire)
        else:
            lcp = _estimated_section_lcp(previous, fingerprint)
        trace = CacheLayoutTrace(
            step=step,
            provider=self.redactor.redact_text(provider),
            request_fingerprint=fingerprint.request_wire_fingerprint,
            provider_fingerprint=fingerprint.provider_fingerprint,
            epoch_fingerprint=fingerprint.epoch_fingerprint,
            section_fingerprints=fingerprint.section_fingerprints,
            section_byte_lengths={
                name: getattr(fingerprint, name).byte_length
                for name in (*_SECTION_ORDER, "dynamic_system_prefix")
            },
            stable_prefix_bytes=fingerprint.stable_prefix_bytes,
            stable_prefix_tokens=fingerprint.stable_prefix_tokens,
            longest_common_prefix_bytes=lcp,
            longest_common_prefix_tokens=_estimate_tokens_from_bytes(lcp),
            first_change_section=first_change,
            reasons=reasons,
            primary_reason=_primary_reason(reasons or [CacheLayoutReason.UNKNOWN]),
            request_id=request_id,
            legacy_lcp_basis=(
                "section_estimate"
                if previous is not None and self._previous_wire is None
                else "canonical_json_estimate"
            ),
            **self._message_comparison(fingerprint, request_kind),
        )
        if request_kind == "ordinary":
            self._previous_ordinary = fingerprint
            self._previous_ordinary_trace = trace
        self._previous = fingerprint
        self._previous_wire = wire
        return trace

    def _message_comparison(
        self,
        current: CacheRequestFingerprint,
        request_kind: str,
    ) -> dict[str, Any]:
        previous = self._previous_ordinary
        current_hashes = current.message_fingerprints or ()
        fields: dict[str, Any] = {"message_count": len(current_hashes)}
        kind = (
            "compression"
            if request_kind == "compression"
            else "cold_start"
            if previous is None
            else "epoch_boundary"
            if previous.epoch_fingerprint != current.epoch_fingerprint
            else "ordinary"
        )
        fields["comparison_kind"] = kind
        if request_kind == "compression" and self._previous_ordinary_trace is not None:
            fields["source_request_id"] = self._previous_ordinary_trace.request_id
        if previous is not None and previous.message_fingerprints is None:
            return fields  # Old checkpoints cannot prove a message-level prefix.
        fields["metric_basis"] = "normalized_messages_v1"
        old_hashes = previous.message_fingerprints or () if previous is not None else ()
        common = 0
        for old, new in zip(old_hashes, current_hashes, strict=False):
            if old != new:
                break
            common += 1
        fields.update(
            previous_message_count=len(old_hashes),
            common_prefix_message_count=common,
            common_prefix_estimated_tokens=sum((current.message_estimated_tokens or ())[:common]),
        )
        if previous is None:
            return fields
        prefix = common == len(old_hashes)
        tools_same = previous.ordered_tools_fingerprint == current.ordered_tools_fingerprint
        binding_same = previous.binding_fingerprint == current.binding_fingerprint
        fields.update(
            previous_request_is_prefix=prefix,
            tools_unchanged=tools_same,
            binding_unchanged=binding_same,
            first_changed_message_index=None if prefix else common,
        )
        if not binding_same:
            fields["prefix_break_reason"] = "binding_change"
        elif not tools_same:
            fields["prefix_break_reason"] = "tools_change"
        elif not prefix and kind != "epoch_boundary":
            fields["prefix_break_reason"] = "history_rewrite"
        return fields

    # Common spelling for call sites and integrations.
    record = observe

    def finalize(self, trace: CacheLayoutTrace, usage: ModelUsage) -> CacheLayoutTrace:
        return trace.with_usage(usage, miss_threshold_tokens=self.miss_threshold_tokens)

    def _structural_reasons(
        self,
        previous: CacheRequestFingerprint | None,
        current: CacheRequestFingerprint,
    ) -> tuple[list[CacheLayoutReason], str | None]:
        if previous is None:
            return [CacheLayoutReason.COLD_START], None
        reasons: list[CacheLayoutReason] = []
        first_change: str | None = None
        changed = {
            name: getattr(previous, name).fingerprint != getattr(current, name).fingerprint
            for name in _SECTION_ORDER
        }
        if previous.provider_fingerprint != current.provider_fingerprint:
            reasons.append(CacheLayoutReason.MODEL_OR_THINKING_CHANGE)
        if previous.epoch_fingerprint != current.epoch_fingerprint:
            reasons.append(CacheLayoutReason.EPOCH_ROLLOVER)
            first_change = "epoch_snapshot"
        if changed["system_instructions"] or (
            previous.dynamic_system_prefix.fingerprint != current.dynamic_system_prefix.fingerprint
        ):
            reasons.append(CacheLayoutReason.DYNAMIC_SYSTEM_PREFIX)
            first_change = "system_instructions"
        if changed["task_project_snapshot"]:
            reasons.append(CacheLayoutReason.PROJECT_SNAPSHOT_CHANGE)
            first_change = first_change or "task_project_snapshot"
        if changed["tool_schema"]:
            reasons.append(CacheLayoutReason.TOOL_SCHEMA_CHANGE)
            first_change = first_change or "tool_schema"
        if changed["memory_projection"]:
            reasons.append(CacheLayoutReason.MEMORY_PROJECTION_CHANGE)
            first_change = first_change or "memory_projection"
        if changed["history_groups"]:
            if not _is_append_only(
                previous.history_group_fingerprints,
                current.history_group_fingerprints,
            ):
                reasons.append(CacheLayoutReason.HISTORY_RESELECTION)
            first_change = first_change or "history_groups"
        if not reasons and changed["request"]:
            reasons.append(CacheLayoutReason.UNKNOWN)
            first_change = first_change or "request"
        return _ordered_unique(reasons), first_change


def fingerprint_text(value: str, *, redactor: SecretRedactor | None = None) -> str:
    """Hash redacted text; the original value is never returned or persisted."""

    safe = (redactor or SecretRedactor()).redact_text(value)
    return _sha256(safe.encode("utf-8"))


def fingerprint_json(value: object, *, redactor: SecretRedactor | None = None) -> str:
    """Hash a canonical, recursively redacted JSON-compatible value."""

    safe = (redactor or SecretRedactor()).redact(value)
    return _sha256(_canonical_json(safe).encode("utf-8"))


def fingerprint_request(
    messages: Sequence[ModelMessage],
    tools: Sequence[ToolSpec],
    *,
    redactor: SecretRedactor | None = None,
    model: str | None = None,
    thinking: object | None = None,
    epoch_snapshot: object | None = None,
    system_instructions: str | None = None,
    task_project_snapshot: object | None = None,
    memory_projection: object | None = None,
    binding_fingerprint: str | None = None,
    provider: str | None = None,
) -> tuple[CacheRequestFingerprint, bytes]:
    """Return safe section fingerprints and ephemeral canonical request bytes.

    The bytes are returned only so an in-process diagnostics session can compute
    an exact common prefix.  They are intentionally not part of any Pydantic
    model and are never written to events or checkpoints.
    """

    redactor = redactor or SecretRedactor()
    continuation_codec = ContinuationCodec(redactor)
    safe_messages = [
        continuation_codec.to_public(message.model_dump(mode="json")) for message in messages
    ]
    safe_tools = [redactor.redact(tool.model_dump(mode="json")) for tool in tools]
    # Preserve the legacy canonical JSON estimate; PPS separately hashes actual order.
    safe_tools = sorted(safe_tools, key=lambda tool: str(tool["name"]))
    actual_system = safe_messages[0].get("content", "") if safe_messages else ""
    actual_project = safe_messages[1].get("content", "") if len(safe_messages) > 1 else ""
    system_value = actual_system if system_instructions is None else system_instructions
    project_value = actual_project if task_project_snapshot is None else task_project_snapshot
    history = safe_messages[2:] if len(safe_messages) > 2 else []
    history_groups = _history_groups(history)
    section_values: dict[str, object] = {
        "system_instructions": system_value,
        "task_project_snapshot": project_value,
        "tool_schema": safe_tools,
        "epoch_snapshot": epoch_snapshot if epoch_snapshot is not None else "",
        "memory_projection": memory_projection if memory_projection is not None else "",
        "history_groups": history_groups,
    }
    section_values = redactor.redact(section_values)
    sections = {name: _section_fingerprint(value) for name, value in section_values.items()}
    dynamic_system = _section_fingerprint(actual_system)
    safe_model = redactor.redact_text(model or "")
    safe_thinking = redactor.redact(thinking)
    provider_fingerprint = fingerprint_json(
        {"provider_model": safe_model, "thinking": safe_thinking}, redactor=redactor
    )
    epoch_fingerprint = fingerprint_json(section_values["epoch_snapshot"], redactor=redactor)
    request_payload = {
        "model": safe_model,
        "thinking": safe_thinking,
        "messages": safe_messages,
        "tools": safe_tools,
    }
    wire = _canonical_json(request_payload).encode("utf-8")
    request_section = _section_fingerprint(request_payload)
    return (
        CacheRequestFingerprint(
            system_instructions=sections["system_instructions"],
            task_project_snapshot=sections["task_project_snapshot"],
            tool_schema=sections["tool_schema"],
            epoch_snapshot=sections["epoch_snapshot"],
            memory_projection=sections["memory_projection"],
            history_groups=sections["history_groups"],
            request=request_section,
            dynamic_system_prefix=dynamic_system,
            history_group_fingerprints=tuple(
                fingerprint_json(group, redactor=redactor) for group in history_groups
            ),
            provider_fingerprint=provider_fingerprint,
            epoch_fingerprint=epoch_fingerprint,
            request_wire_fingerprint=_sha256(wire),
            message_fingerprints=tuple(
                _sha256(_canonical_json(message.model_dump(mode="json")).encode("utf-8"))
                for message in messages
            ),
            message_estimated_tokens=tuple(ContextEngine.estimate_message(m) for m in messages),
            ordered_tools_fingerprint=_sha256(
                _canonical_json([tool.model_dump(mode="json") for tool in tools]).encode("utf-8")
            ),
            binding_fingerprint=_sha256(
                _canonical_json(
                    {
                        "binding": binding_fingerprint,
                        "provider": provider,
                        "model": model,
                        "thinking": thinking,
                    }
                ).encode("utf-8")
            ),
        ),
        wire,
    )


def _history_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "assistant" and current:
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)
    return groups


def _section_fingerprint(value: object) -> CacheSectionFingerprint:
    encoded = _canonical_json(value).encode("utf-8")
    return CacheSectionFingerprint(
        fingerprint=_sha256(encoded),
        byte_length=len(encoded),
        estimated_tokens=_estimate_tokens_from_bytes(len(encoded)),
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((_canonical_value(item) for item in value), key=_canonical_json)
    return value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _estimate_tokens_from_bytes(byte_length: int) -> int:
    return 0 if byte_length <= 0 else (byte_length + 2) // 3


def _longest_common_prefix(first: bytes, second: bytes) -> int:
    limit = min(len(first), len(second))
    index = 0
    while index < limit and first[index] == second[index]:
        index += 1
    return index


def _estimated_section_lcp(
    previous: CacheRequestFingerprint,
    current: CacheRequestFingerprint,
) -> int:
    length = 0
    for name in _SECTION_ORDER:
        old = getattr(previous, name)
        new = getattr(current, name)
        if old.fingerprint != new.fingerprint:
            break
        length += min(old.byte_length, new.byte_length)
    return length


def _is_append_only(previous: Sequence[str], current: Sequence[str]) -> bool:
    return len(current) >= len(previous) and tuple(current[: len(previous)]) == tuple(previous)


def _ordered_unique(reasons: Sequence[CacheLayoutReason]) -> list[CacheLayoutReason]:
    return list(dict.fromkeys(reasons))


def _primary_reason(reasons: Sequence[CacheLayoutReason]) -> CacheLayoutReason:
    return min(
        reasons,
        key=lambda reason: (
            _REASON_PRIORITY.index(reason) if reason in _REASON_PRIORITY else len(_REASON_PRIORITY)
        ),
    )


# Short aliases keep the public surface convenient for integrations.
PromptCacheDiagnostics = CacheDiagnostics
PromptCacheDiagnosticsSnapshot = CacheDiagnosticsSnapshot


__all__ = [
    "CacheDiagnostics",
    "CacheDiagnosticsSnapshot",
    "CacheLayoutReason",
    "CacheLayoutTrace",
    "CacheRequestFingerprint",
    "CacheSectionFingerprint",
    "PromptCacheDiagnostics",
    "PromptCacheDiagnosticsSnapshot",
    "fingerprint_json",
    "fingerprint_request",
    "fingerprint_text",
]
