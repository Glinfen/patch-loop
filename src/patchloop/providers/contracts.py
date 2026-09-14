"""Provider gateway configuration and error contracts.

This module intentionally has no dependency on PatchLoop's domain or persistence
layers.  Provider adapters can therefore use these types without introducing an
import cycle back into the runtime.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class ProviderProtocol(StrEnum):
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"


class ChatDialect(StrEnum):
    STANDARD = "standard"
    DEEPSEEK = "deepseek"


class ProviderAuth(StrEnum):
    BEARER = "bearer"
    NONE = "none"


class ReasoningTransport(StrEnum):
    NONE = "none"
    DEEPSEEK_TEXT = "deepseek_text"
    RESPONSES_ITEMS = "responses_items"


class ProviderCapabilities(BaseModel):
    """Capabilities declared by a configured model, never inferred from its name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tools: bool = False
    multiple_tool_calls: bool = False
    streaming: bool = False
    reasoning_transport: ReasoningTransport = ReasoningTransport.NONE
    structured_output: bool = False
    context_window_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    usage_supported: bool = False
    cache_usage_supported: bool = False

    @model_validator(mode="after")
    def validate_combinations(self) -> Self:
        if self.multiple_tool_calls and not self.tools:
            raise ValueError("multiple_tool_calls requires tools")
        if self.cache_usage_supported and not self.usage_supported:
            raise ValueError("cache_usage_supported requires usage_supported")
        if self.max_output_tokens > self.context_window_tokens:
            raise ValueError("max_output_tokens cannot exceed context_window_tokens")
        return self


class ProviderGeneration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_output_tokens: int = Field(gt=0)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    reasoning_enabled: bool = False
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=64)
    token_limit_field: str = Field(
        default="max_tokens",
        pattern=r"^(max_tokens|max_completion_tokens)$",
    )

    @model_validator(mode="after")
    def validate_reasoning(self) -> Self:
        if not self.reasoning_enabled and self.reasoning_effort is not None:
            raise ValueError("reasoning_effort requires reasoning_enabled")
        return self


class ProviderTransportConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    streaming: bool = True
    connect_timeout_seconds: float = Field(default=10.0, gt=0)
    write_timeout_seconds: float = Field(default=30.0, gt=0)
    idle_timeout_seconds: float = Field(default=60.0, gt=0)
    total_timeout_seconds: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)
    max_sse_frame_bytes: int = Field(default=1_048_576, ge=1, le=16_777_216)
    max_response_bytes: int = Field(default=16_777_216, ge=1, le=134_217_728)
    proxy_url: str | None = None
    ca_bundle: str | None = None

    @model_validator(mode="after")
    def validate_deadline(self) -> Self:
        if self.total_timeout_seconds < self.connect_timeout_seconds:
            raise ValueError("total timeout cannot be shorter than connect timeout")
        return self


class ProviderPricing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1, max_length=128)
    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)
    cached_input_per_million: float | None = Field(default=None, ge=0)


class ValidatedResponseItem(BaseModel):
    """A bounded, provider-native Responses continuation item.

    The type-specific field whitelist is checked by this model and again when the
    adapter replays it, so restored state cannot smuggle arbitrary response data.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str = Field(pattern=r"^(message|function_call|reasoning)$")
    item: dict[str, Any]

    @model_validator(mode="after")
    def validate_native_item(self) -> Self:
        fields_by_type = {
            "message": {"id", "type", "status", "role", "content"},
            "function_call": {"id", "type", "status", "call_id", "name", "arguments"},
            "reasoning": {"id", "type", "status", "summary", "encrypted_content"},
        }
        allowed = fields_by_type[self.type]
        if self.item.get("type") != self.type or set(self.item) - allowed:
            raise ValueError("Responses continuation item contains unsupported fields")
        if not isinstance(self.item.get("id"), str) or not self.item["id"]:
            raise ValueError("Responses continuation item requires an item ID")
        status = self.item.get("status")
        if status is not None and status != "completed":
            raise ValueError("Responses continuation item is not complete")
        if self.type == "message":
            content = self.item.get("content")
            if self.item.get("role") != "assistant" or not isinstance(content, list):
                raise ValueError("Responses continuation message is invalid")
            for part in content:
                if not isinstance(part, dict) or set(part) - {
                    "type",
                    "text",
                    "refusal",
                    "annotations",
                    "logprobs",
                }:
                    raise ValueError("Responses continuation message content is invalid")
                part_type = part.get("type")
                if part_type == "output_text" and not isinstance(part.get("text"), str):
                    raise ValueError("Responses continuation text is invalid")
                if part_type == "output_text" and "refusal" in part:
                    raise ValueError("Responses continuation text has refusal data")
                if part_type == "refusal" and not isinstance(part.get("refusal"), str):
                    raise ValueError("Responses continuation refusal is invalid")
                if part_type == "refusal" and "text" in part:
                    raise ValueError("Responses continuation refusal has text data")
                if not isinstance(part_type, str) or part_type not in {
                    "output_text",
                    "refusal",
                }:
                    raise ValueError("Responses continuation message type is unsupported")
        elif self.type == "function_call":
            for field_name in ("call_id", "name", "arguments"):
                if not isinstance(self.item.get(field_name), str):
                    raise ValueError("Responses continuation function call is invalid")
            if not self.item["call_id"] or not self.item["name"]:
                raise ValueError("Responses continuation function call is incomplete")
        else:
            encrypted_content = self.item.get("encrypted_content")
            if not isinstance(encrypted_content, str) or not encrypted_content:
                raise ValueError("Responses continuation reasoning is not replayable")
            summary = self.item.get("summary", [])
            if not isinstance(summary, list) or any(
                not isinstance(part, dict)
                or part.get("type") != "summary_text"
                or not isinstance(part.get("text"), str)
                or set(part) - {"type", "text"}
                for part in summary
            ):
                raise ValueError("Responses continuation reasoning summary is invalid")
        return self


class ProviderContinuation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    deepseek_reasoning_content: str | None = None
    responses_items: tuple[ValidatedResponseItem, ...] = ()
    replayable: bool = True

    @model_validator(mode="after")
    def validate_single_protocol(self) -> Self:
        if self.deepseek_reasoning_content is not None and self.responses_items:
            raise ValueError("continuation cannot mix DeepSeek text and Responses items")
        return self


class ProviderBinding(BaseModel):
    """Immutable, non-secret snapshot of the provider selected for a task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(default=1, ge=1, le=1)
    profile_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    protocol: ProviderProtocol
    dialect: ChatDialect = ChatDialect.STANDARD
    model: str = Field(min_length=1, max_length=256)
    base_url: str = Field(min_length=1, max_length=2048)
    auth: ProviderAuth = ProviderAuth.BEARER
    credential_env: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    capabilities: ProviderCapabilities
    generation: ProviderGeneration
    transport: ProviderTransportConfig = Field(default_factory=ProviderTransportConfig)
    pricing: ProviderPricing | None = None
    fingerprint: str = ""

    @model_validator(mode="after")
    def validate_and_fingerprint(self) -> Self:
        if self.auth is ProviderAuth.BEARER and not self.credential_env:
            raise ValueError("bearer auth requires credential_env")
        if self.auth is ProviderAuth.NONE and self.credential_env is not None:
            raise ValueError("auth=none cannot define credential_env")
        if self.protocol is ProviderProtocol.RESPONSES and self.dialect is not ChatDialect.STANDARD:
            raise ValueError("Responses protocol does not support a Chat dialect")
        if self.capabilities.reasoning_transport is ReasoningTransport.DEEPSEEK_TEXT and not (
            self.protocol is ProviderProtocol.CHAT_COMPLETIONS
            and self.dialect is ChatDialect.DEEPSEEK
        ):
            raise ValueError("deepseek_text reasoning requires the DeepSeek Chat dialect")
        if (
            self.capabilities.reasoning_transport is ReasoningTransport.RESPONSES_ITEMS
            and self.protocol is not ProviderProtocol.RESPONSES
        ):
            raise ValueError("responses_items reasoning requires the Responses protocol")
        if self.generation.max_output_tokens > self.capabilities.max_output_tokens:
            raise ValueError("generation max_output_tokens exceeds declared capability")

        expected = self.compute_fingerprint()
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("provider binding fingerprint does not match configuration")
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", expected)
        return self

    def compute_fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"fingerprint"})
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ProviderErrorKind(StrEnum):
    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"
    TRUNCATED = "truncated"
    CANCELLED = "cancelled"
    CAPABILITY = "capability"
    TRANSPORT_CLEANUP_FAILED = "transport_cleanup_failed"
    OBSERVER = "observer_error"
    STRUCTURED_OUTPUT_INVALID = "structured_output_invalid"
    RESPONSE_TOO_LARGE = "response_too_large"
    CONTINUATION_UNAVAILABLE = "continuation_unavailable"
    REFUSAL = "refusal"
    UNSUPPORTED_OUTPUT = "unsupported_output"


class ProviderError(Exception):
    """A sanitized provider failure safe to persist or show to a user."""

    def __init__(
        self,
        kind: ProviderErrorKind | str,
        safe_message: str,
        *,
        http_status: int | None = None,
        retry_after: float | None = None,
        request_sent: bool = False,
        partial_output: bool = False,
        retryable: bool = False,
        usage_unknown: bool = False,
    ) -> None:
        self.kind = ProviderErrorKind(kind)
        self.safe_message = safe_message
        self.http_status = http_status
        self.retry_after = retry_after
        self.request_sent = request_sent
        self.partial_output = partial_output
        self.retryable = retryable
        self.usage_unknown = usage_unknown
        super().__init__(safe_message)

    def to_dict(self) -> dict[str, str | int | float | bool | None]:
        return {
            "kind": self.kind.value,
            "safe_message": self.safe_message,
            "http_status": self.http_status,
            "retry_after": self.retry_after,
            "request_sent": self.request_sent,
            "partial_output": self.partial_output,
            "retryable": self.retryable,
            "usage_unknown": self.usage_unknown,
        }


def resolve_credential(binding: ProviderBinding, environment: dict[str, str]) -> SecretStr | None:
    """Resolve a credential without ever adding its value to a binding."""

    if binding.auth is ProviderAuth.NONE:
        return None
    assert binding.credential_env is not None
    value = environment.get(binding.credential_env)
    if not value:
        raise ProviderError(
            ProviderErrorKind.CONFIGURATION,
            f"credential environment variable {binding.credential_env} is not set",
        )
    return SecretStr(value)
