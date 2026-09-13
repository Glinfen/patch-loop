"""Provider-neutral model protocol."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from patchloop.domain import ToolCall
from patchloop.providers.contracts import ProviderBinding, ProviderContinuation


class ModelMessage(BaseModel):
    role: str
    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    continuation: ProviderContinuation | None = None

    @model_serializer(mode="wrap")
    def serialize_compatibly(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> Any:
        payload = handler(self)
        if self.continuation is None:
            payload.pop("continuation", None)
        return payload


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]
    permission: str = "read"


class ModelUsage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    input_tokens_reported: bool | None = None
    output_tokens_reported: bool | None = None
    cost_status: Literal["estimated", "unknown", "legacy"] = "legacy"
    pricing_version: str | None = None

    @model_serializer(mode="wrap")
    def serialize_compatibly(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> Any:
        payload = handler(self)
        if self.input_tokens_reported is None:
            payload.pop("input_tokens_reported", None)
        if self.output_tokens_reported is None:
            payload.pop("output_tokens_reported", None)
        if self.cost_status == "legacy":
            payload.pop("cost_status", None)
        if self.pricing_version is None:
            payload.pop("pricing_version", None)
        return payload


class ModelResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    continuation: ProviderContinuation | None = None
    request_id: str | None = None
    finish_reason: str | None = None

    @model_serializer(mode="wrap")
    def serialize_compatibly(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> Any:
        payload = handler(self)
        if self.continuation is None:
            payload.pop("continuation", None)
        if self.request_id is None:
            payload.pop("request_id", None)
        if self.finish_reason is None:
            payload.pop("finish_reason", None)
        return payload


class ProviderRequestPurpose(StrEnum):
    AGENT_STEP = "agent_step"
    EPOCH_COMPRESSION = "epoch_compression"


class ProviderRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=64)
    step_index: int = Field(ge=0)
    purpose: ProviderRequestPurpose
    epoch_generation: int = Field(default=0, ge=0)
    input_revision: int = Field(default=0, ge=0)
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolSpec, ...] = ()
    output_schema: dict[str, Any] | None = None
    max_output_tokens: int | None = Field(default=None, gt=0)


class ProviderEventType(StrEnum):
    REQUEST_STARTED = "request_started"
    ATTEMPT_STARTED = "attempt_started"
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    USAGE = "usage"
    RESPONSE_COMPLETED = "response_completed"
    REQUEST_FAILED = "request_failed"
    REQUEST_CANCELLED = "request_cancelled"


class ProviderEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: ProviderEventType
    request_id: str = Field(min_length=1, max_length=256)
    attempt_id: str | None = Field(default=None, min_length=1, max_length=256)
    sequence: int = Field(ge=0)
    delta: str | None = None
    tool_call_index: int | None = Field(default=None, ge=0)
    usage: ModelUsage | None = None
    response: ModelResponse | None = None
    error_kind: str | None = None
    safe_message: str | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> ProviderEvent:
        delta_events = {
            ProviderEventType.TEXT_DELTA,
            ProviderEventType.REASONING_DELTA,
            ProviderEventType.TOOL_CALL_DELTA,
        }
        if self.type in delta_events and self.delta is None:
            raise ValueError(f"{self.type.value} requires delta")
        if self.type is ProviderEventType.USAGE and self.usage is None:
            raise ValueError("usage event requires usage")
        if self.type is ProviderEventType.RESPONSE_COMPLETED and self.response is None:
            raise ValueError("response_completed event requires response")
        if self.type is not ProviderEventType.RESPONSE_COMPLETED and self.response is not None:
            raise ValueError("only response_completed may carry a response")
        if self.type is ProviderEventType.REQUEST_FAILED and not self.error_kind:
            raise ValueError("request_failed event requires error_kind")
        return self


type ControlAction = Literal["pause", "cancel", "lease_lost"]
type ProviderControl = Callable[[], ControlAction | None]
type ProviderEventObserver = Callable[[ProviderEvent], None]


class EncodedRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["POST"] = "POST"
    path: str = Field(min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any]
    stream: bool = False


class StreamReducer(Protocol):
    def feed(self, frame: object) -> list[ProviderEvent]: ...

    def finish(self) -> ModelResponse: ...


class ProviderAdapter(Protocol):
    def encode(self, request: ProviderRequest, binding: ProviderBinding) -> EncodedRequest: ...

    def parse_json(self, body: dict[str, Any], binding: ProviderBinding) -> ModelResponse: ...

    def new_reducer(self, binding: ProviderBinding) -> StreamReducer: ...


class ProviderGateway(Protocol):
    def complete_request(
        self,
        request: ProviderRequest,
        *,
        control: ProviderControl | None = None,
        on_event: ProviderEventObserver | None = None,
    ) -> ModelResponse: ...


class ModelProvider(Protocol):
    @property
    def name(self) -> str: ...

    def complete(
        self,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
    ) -> ModelResponse: ...
