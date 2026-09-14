from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

import pytest

from patchloop.domain import ToolCall
from patchloop.providers.base import (
    EncodedRequest,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    ProviderEvent,
    ProviderEventType,
    ProviderRequest,
    ProviderRequestPurpose,
    ToolSpec,
)
from patchloop.providers.contracts import (
    ChatDialect,
    ProviderAuth,
    ProviderBinding,
    ProviderCapabilities,
    ProviderError,
    ProviderErrorKind,
    ProviderGeneration,
    ProviderProtocol,
    ProviderTransportConfig,
)
from patchloop.providers.fake import FakeProvider
from patchloop.providers.gateway import LegacyProviderAdapter, ProviderGateway
from patchloop.providers.sse import SSEFrame
from patchloop.providers.transport import TransportControlError


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
        error: Exception | None = None,
        block: bool = False,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self.chunks = chunks or [b"{}"]
        self.error = error
        self.block = block
        self.closed = False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error
        if self.block:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = list(responses)
        self.open_count = 0
        self.scope_count = 0
        self.scope_closed = False
        self.encoded: list[EncodedRequest] = []
        self.closed_responses: list[FakeResponse] = []

    @asynccontextmanager
    async def client_scope(self) -> AsyncIterator[None]:
        self.scope_count += 1
        try:
            yield
        finally:
            self.scope_closed = True

    @asynccontextmanager
    async def open(
        self,
        encoded: EncodedRequest,
        timeouts: ProviderTransportConfig,
        *,
        control: Any = None,
    ) -> AsyncIterator[FakeResponse]:
        del timeouts, control
        self.open_count += 1
        self.encoded.append(encoded)
        if not self.responses:
            raise AssertionError("unexpected provider attempt")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        try:
            yield response
        finally:
            await response.aclose()
            self.closed_responses.append(response)


class FakeReducer:
    def __init__(self, response: ModelResponse, *, duplicate_terminal: bool = False) -> None:
        self.response = response
        self.duplicate_terminal = duplicate_terminal
        self.finished = False

    def feed(self, frame: object) -> list[ProviderEvent]:
        assert isinstance(frame, SSEFrame)
        if frame.data == "[DONE]":
            if self.finished:
                raise ProviderError(ProviderErrorKind.PROTOCOL, "duplicate terminal")
            self.finished = True
            if self.duplicate_terminal:
                raise ProviderError(ProviderErrorKind.PROTOCOL, "duplicate terminal")
            return []
        if frame.data == "usage":
            return [
                ProviderEvent(
                    type=ProviderEventType.USAGE,
                    request_id="adapter-request",
                    sequence=0,
                    usage=ModelUsage(input_tokens=2),
                )
            ]
        return [
            ProviderEvent(
                type=ProviderEventType.TEXT_DELTA,
                request_id="adapter-request",
                sequence=0,
                delta=frame.data,
            )
        ]

    def finish(self) -> ModelResponse:
        if not self.finished:
            raise ProviderError(
                ProviderErrorKind.TRUNCATED,
                "stream has no terminal frame",
                request_sent=True,
                partial_output=True,
            )
        return self.response


class FakeAdapter:
    def __init__(
        self,
        *,
        response: ModelResponse | None = None,
        stream_response: ModelResponse | None = None,
        duplicate_terminal: bool = False,
        force_stream: bool | None = None,
    ) -> None:
        self.response = response or ModelResponse(content="complete", finish_reason="stop")
        self.stream_response = stream_response or ModelResponse(
            content="stream complete", finish_reason="stop"
        )
        self.duplicate_terminal = duplicate_terminal
        self.force_stream = force_stream

    def encode(self, request: ProviderRequest, binding: ProviderBinding) -> EncodedRequest:
        del request
        stream = (
            self.force_stream
            if self.force_stream is not None
            else binding.transport.streaming and binding.capabilities.streaming
        )
        return EncodedRequest(path="/chat/completions", body={"stream": stream}, stream=stream)

    def parse_json(self, body: dict[str, Any], binding: ProviderBinding) -> ModelResponse:
        del body, binding
        return self.response

    def new_reducer(self, binding: ProviderBinding) -> FakeReducer:
        del binding
        return FakeReducer(self.stream_response, duplicate_terminal=self.duplicate_terminal)


def make_binding(
    *,
    capabilities: ProviderCapabilities | None = None,
    streaming: bool = False,
    max_retries: int = 2,
    total_timeout: float = 5.0,
) -> ProviderBinding:
    return ProviderBinding(
        profile_id="test",
        protocol=ProviderProtocol.CHAT_COMPLETIONS,
        dialect=ChatDialect.STANDARD,
        model="test-model",
        base_url="https://provider.example",
        auth=ProviderAuth.NONE,
        capabilities=capabilities
        or ProviderCapabilities(
            tools=True,
            multiple_tool_calls=True,
            streaming=True,
            structured_output=True,
            context_window_tokens=4096,
            max_output_tokens=512,
            usage_supported=True,
        ),
        generation=ProviderGeneration(max_output_tokens=128),
        transport=ProviderTransportConfig(
            streaming=streaming,
            connect_timeout_seconds=min(0.1, total_timeout),
            total_timeout_seconds=total_timeout,
            max_retries=max_retries,
        ),
    )


def make_request(
    *,
    tools: tuple[ToolSpec, ...] = (),
    output_schema: dict[str, Any] | None = None,
    max_output_tokens: int | None = None,
) -> ProviderRequest:
    return ProviderRequest(
        request_id="request-1",
        task_id="task-1",
        step_index=0,
        purpose=ProviderRequestPurpose.AGENT_STEP,
        messages=(ModelMessage(role="user", content="hello"),),
        tools=tools,
        output_schema=output_schema,
        max_output_tokens=max_output_tokens,
    )


def make_gateway(
    transport: FakeTransport,
    *,
    binding: ProviderBinding | None = None,
    adapter: FakeAdapter | None = None,
    clock: FakeClock | None = None,
) -> ProviderGateway:
    selected_clock = clock or FakeClock()
    return ProviderGateway(
        binding or make_binding(),
        adapter or FakeAdapter(),
        transport,
        clock=selected_clock,
        sleep=selected_clock.sleep,
        random_source=lambda: 0.0,
        wall_clock=lambda: datetime(2026, 9, 14, tzinfo=UTC),
    )


def test_json_response_emits_one_complete_lifecycle_and_closes_scope() -> None:
    transport = FakeTransport([FakeResponse()])
    adapter = FakeAdapter(response=ModelResponse(content="json answer", finish_reason="stop"))
    events: list[ProviderEvent] = []

    result = make_gateway(transport, adapter=adapter).complete_request(
        make_request(), on_event=events.append
    )

    assert result.content == "json answer"
    assert result.request_id == "request-1"
    assert [event.type for event in events] == [
        ProviderEventType.REQUEST_STARTED,
        ProviderEventType.ATTEMPT_STARTED,
        ProviderEventType.RESPONSE_COMPLETED,
    ]
    assert [event.sequence for event in events] == [0, 1, 2]
    assert transport.encoded[0].stream is False
    assert transport.open_count == 1
    assert transport.scope_count == 1
    assert transport.scope_closed
    assert transport.closed_responses[0].closed


def test_repeated_logical_request_invocations_get_fresh_attempt_ids() -> None:
    transport = FakeTransport([FakeResponse(), FakeResponse()])
    events: list[ProviderEvent] = []
    gateway = make_gateway(transport)

    gateway.complete_request(make_request(), on_event=events.append)
    gateway.complete_request(make_request(), on_event=events.append)

    attempts = [event for event in events if event.type is ProviderEventType.ATTEMPT_STARTED]
    assert attempts[0].attempt_id != attempts[1].attempt_id


def test_sse_requires_terminal_and_forwards_only_delta_events() -> None:
    response = FakeResponse(
        headers={"content-type": "text/event-stream; charset=utf-8"},
        chunks=[b"data: hello\n\n", b"data: usage\n\n", b"data: [DONE]\n\n"],
    )
    transport = FakeTransport([response])
    adapter = FakeAdapter(
        stream_response=ModelResponse(
            content="hello",
            usage=ModelUsage(input_tokens=2),
            finish_reason="stop",
        )
    )
    events: list[ProviderEvent] = []

    result = make_gateway(
        transport,
        binding=make_binding(streaming=True),
        adapter=adapter,
    ).complete_request(make_request(), on_event=events.append)

    assert result.content == "hello"
    assert [event.type for event in events] == [
        ProviderEventType.REQUEST_STARTED,
        ProviderEventType.ATTEMPT_STARTED,
        ProviderEventType.TEXT_DELTA,
        ProviderEventType.USAGE,
        ProviderEventType.RESPONSE_COMPLETED,
    ]
    assert events[2].delta == "hello"
    assert events[2].request_id == "request-1"
    assert events[2].attempt_id == events[1].attempt_id
    assert sum(event.type is ProviderEventType.USAGE for event in events) == 1


@pytest.mark.parametrize(
    ("provider_request", "binding", "expected_kind"),
    [
        (
            make_request(tools=(ToolSpec(name="run", description="run", parameters={}),)),
            make_binding(
                capabilities=ProviderCapabilities(context_window_tokens=4096, max_output_tokens=512)
            ),
            ProviderErrorKind.CAPABILITY,
        ),
        (
            make_request(
                tools=(ToolSpec(name="run", description="run", parameters={}),),
                output_schema={"type": "object"},
            ),
            make_binding(),
            ProviderErrorKind.CAPABILITY,
        ),
        (
            make_request(output_schema={"type": "not-a-json-schema-type"}),
            make_binding(),
            ProviderErrorKind.CONFIGURATION,
        ),
        (
            make_request(max_output_tokens=2048),
            make_binding(),
            ProviderErrorKind.CAPABILITY,
        ),
    ],
)
def test_preflight_capability_and_schema_errors_do_not_open_transport(
    provider_request: ProviderRequest,
    binding: ProviderBinding,
    expected_kind: ProviderErrorKind,
) -> None:
    transport = FakeTransport([])

    with pytest.raises(ProviderError) as error:
        make_gateway(transport, binding=binding).complete_request(provider_request)

    assert error.value.kind is expected_kind
    assert transport.open_count == 0


@pytest.mark.parametrize(
    ("content", "expected_kind"),
    [
        ('{"count": 3}', None),
        ('{"count": "three"}', ProviderErrorKind.STRUCTURED_OUTPUT_INVALID),
        ('{"count": NaN}', ProviderErrorKind.STRUCTURED_OUTPUT_INVALID),
        ("not json", ProviderErrorKind.STRUCTURED_OUTPUT_INVALID),
    ],
)
def test_structured_output_is_checked_locally_after_complete_response(
    content: str,
    expected_kind: ProviderErrorKind | None,
) -> None:
    transport = FakeTransport([FakeResponse()])
    adapter = FakeAdapter(response=ModelResponse(content=content, finish_reason="stop"))
    events: list[ProviderEvent] = []
    request = make_request(
        output_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        }
    )

    gateway = make_gateway(transport, adapter=adapter)
    if expected_kind is None:
        result = gateway.complete_request(request, on_event=events.append)
        assert result.content == content
        assert events[-1].type is ProviderEventType.RESPONSE_COMPLETED
    else:
        with pytest.raises(ProviderError) as error:
            gateway.complete_request(request, on_event=events.append)
        assert error.value.kind is expected_kind
        assert all(event.type is not ProviderEventType.RESPONSE_COMPLETED for event in events)
    assert transport.closed_responses[0].closed


@pytest.mark.parametrize(
    ("status", "retry_after", "expected_sleep"),
    [
        (429, "3", 3.0),
        (
            429,
            format_datetime(datetime(2026, 9, 14, 0, 0, 4, tzinfo=UTC), usegmt=True),
            4.0,
        ),
        (502, None, 1.0),
    ],
)
def test_retry_after_and_whitelisted_502_retry_with_distinct_attempt_ids(
    status: int,
    retry_after: str | None,
    expected_sleep: float,
) -> None:
    clock = FakeClock()
    headers = {"content-type": "application/json"}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    transport = FakeTransport(
        [
            FakeResponse(status_code=status, headers=headers),
            FakeResponse(),
        ]
    )
    events: list[ProviderEvent] = []

    result = make_gateway(transport, clock=clock).complete_request(
        make_request(), on_event=events.append
    )

    attempts = [event for event in events if event.type is ProviderEventType.ATTEMPT_STARTED]
    assert result.content == "complete"
    assert transport.open_count == 2
    assert attempts[0].attempt_id != attempts[1].attempt_id
    assert clock.value == pytest.approx(expected_sleep)


def test_authentication_error_is_not_retried() -> None:
    transport = FakeTransport([FakeResponse(status_code=401), FakeResponse()])

    with pytest.raises(ProviderError) as error:
        make_gateway(transport).complete_request(make_request())

    assert error.value.kind is ProviderErrorKind.AUTHENTICATION
    assert transport.open_count == 1


@pytest.mark.parametrize(
    ("response", "expected_kind"),
    [
        (ModelResponse(content="partial", finish_reason="length"), ProviderErrorKind.TRUNCATED),
        (ModelResponse(), ProviderErrorKind.PROTOCOL),
    ],
)
def test_truncated_and_empty_responses_are_never_returned(
    response: ModelResponse,
    expected_kind: ProviderErrorKind,
) -> None:
    transport = FakeTransport([FakeResponse()])
    adapter = FakeAdapter(response=response)
    events: list[ProviderEvent] = []

    with pytest.raises(ProviderError) as error:
        make_gateway(transport, adapter=adapter).complete_request(
            make_request(), on_event=events.append
        )

    assert error.value.kind is expected_kind
    assert all(event.type is not ProviderEventType.RESPONSE_COMPLETED for event in events)
    assert transport.closed_responses[0].closed


def test_adapter_cannot_enable_unsupported_streaming() -> None:
    capabilities = ProviderCapabilities(context_window_tokens=4096, max_output_tokens=512)
    binding = make_binding(capabilities=capabilities, streaming=False)
    transport = FakeTransport([])

    with pytest.raises(ProviderError) as error:
        make_gateway(
            transport,
            binding=binding,
            adapter=FakeAdapter(force_stream=True),
        ).complete_request(make_request())

    assert error.value.kind is ProviderErrorKind.CAPABILITY
    assert transport.open_count == 0


def test_requested_streaming_requires_declared_capability() -> None:
    capabilities = ProviderCapabilities(context_window_tokens=4096, max_output_tokens=512)
    binding = make_binding(capabilities=capabilities, streaming=True)
    transport = FakeTransport([])

    with pytest.raises(ProviderError) as error:
        make_gateway(transport, binding=binding).complete_request(make_request())

    assert error.value.kind is ProviderErrorKind.CAPABILITY
    assert transport.open_count == 0


def test_configured_reasoning_requires_declared_capability() -> None:
    binding = make_binding().model_copy(
        update={
            "generation": ProviderGeneration(
                max_output_tokens=128,
                reasoning_enabled=True,
            )
        }
    )
    transport = FakeTransport([])

    with pytest.raises(ProviderError) as error:
        make_gateway(transport, binding=binding).complete_request(make_request())

    assert error.value.kind is ProviderErrorKind.CAPABILITY
    assert transport.open_count == 0


def test_disconnect_after_stream_delivery_is_not_retried_and_closes_response() -> None:
    response = FakeResponse(
        headers={"content-type": "text/event-stream"},
        chunks=[b"data: partial\n\n"],
        error=RuntimeError("socket closed"),
    )
    transport = FakeTransport([response, FakeResponse()])
    events: list[ProviderEvent] = []

    with pytest.raises(ProviderError) as error:
        make_gateway(
            transport,
            binding=make_binding(streaming=True),
        ).complete_request(make_request(), on_event=events.append)

    assert error.value.kind is ProviderErrorKind.PROTOCOL
    assert transport.open_count == 1
    assert response.closed
    assert any(event.type is ProviderEventType.TEXT_DELTA for event in events)
    assert all(event.type is not ProviderEventType.RESPONSE_COMPLETED for event in events)


def test_total_deadline_cancels_blocked_body_and_closes_transport() -> None:
    response = FakeResponse(block=True)
    transport = FakeTransport([response])
    binding = make_binding(total_timeout=0.15, max_retries=0)
    gateway = ProviderGateway(
        binding,
        FakeAdapter(),
        transport,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    )

    with pytest.raises(ProviderError) as error:
        gateway.complete_request(make_request())

    assert error.value.kind is ProviderErrorKind.TIMEOUT
    assert response.closed
    assert transport.scope_closed
    assert transport.open_count == 1


def test_cancel_during_backoff_does_not_start_another_attempt() -> None:
    clock = FakeClock()
    transport = FakeTransport(
        [
            FakeResponse(
                status_code=429,
                headers={"content-type": "application/json", "retry-after": "10"},
            ),
            FakeResponse(),
        ]
    )
    cancelled = False

    async def sleep_and_cancel(seconds: float) -> None:
        nonlocal cancelled
        clock.value += seconds
        cancelled = True

    gateway = ProviderGateway(
        make_binding(),
        FakeAdapter(),
        transport,
        clock=clock,
        sleep=sleep_and_cancel,
        random_source=lambda: 0.0,
    )

    with pytest.raises(TransportControlError):
        gateway.complete_request(
            make_request(),
            control=lambda: "cancel" if cancelled else None,
        )

    assert transport.open_count == 1
    assert clock.value == 0.1


def test_observer_failure_aborts_stream_and_closes_response() -> None:
    response = FakeResponse(
        headers={"content-type": "text/event-stream"},
        chunks=[b"data: hello\n\n", b"data: [DONE]\n\n"],
    )
    transport = FakeTransport([response, FakeResponse()])

    def observer(event: ProviderEvent) -> None:
        if event.type is ProviderEventType.TEXT_DELTA:
            raise RuntimeError("ui closed")

    with pytest.raises(ProviderError) as error:
        make_gateway(transport, binding=make_binding(streaming=True)).complete_request(
            make_request(), on_event=observer
        )

    assert error.value.kind is ProviderErrorKind.OBSERVER
    assert transport.open_count == 1
    assert response.closed


def test_invalid_tool_json_is_preserved_for_runtime_feedback() -> None:
    call = ToolCall(
        id="call-1",
        name="run",
        arguments={},
        arguments_error="invalid JSON arguments",
    )
    adapter = FakeAdapter(response=ModelResponse(tool_calls=[call], finish_reason="tool_calls"))
    transport = FakeTransport([FakeResponse()])
    request = make_request(tools=(ToolSpec(name="run", description="run", parameters={}),))

    result = make_gateway(transport, adapter=adapter).complete_request(request)

    assert result.tool_calls[0].arguments_error == "invalid JSON arguments"


@pytest.mark.parametrize(
    "calls",
    [
        [ToolCall(id="", name="run")],
        [ToolCall(id="same", name="run"), ToolCall(id="same", name="run")],
        [ToolCall(id="call-1", name="unrequested")],
        [ToolCall(id="missing-tool-call-id", name="run")],
    ],
)
def test_missing_duplicate_and_unknown_tool_calls_are_rejected(
    calls: list[ToolCall],
) -> None:
    adapter = FakeAdapter(response=ModelResponse(tool_calls=calls, finish_reason="tool_calls"))
    transport = FakeTransport([FakeResponse()])
    request = make_request(tools=(ToolSpec(name="run", description="run", parameters={}),))

    with pytest.raises(ProviderError) as error:
        make_gateway(transport, adapter=adapter).complete_request(request)

    assert error.value.kind is ProviderErrorKind.PROTOCOL
    assert transport.closed_responses[0].closed


def test_duplicate_terminal_frame_is_rejected_without_completion() -> None:
    response = FakeResponse(
        headers={"content-type": "text/event-stream"},
        chunks=[b"data: [DONE]\n\n", b"data: [DONE]\n\n"],
    )
    transport = FakeTransport([response])
    adapter = FakeAdapter(duplicate_terminal=True)
    events: list[ProviderEvent] = []

    with pytest.raises(ProviderError):
        make_gateway(
            transport,
            binding=make_binding(streaming=True),
            adapter=adapter,
        ).complete_request(make_request(), on_event=events.append)

    assert all(event.type is not ProviderEventType.RESPONSE_COMPLETED for event in events)
    assert response.closed


def test_legacy_provider_adapter_keeps_compatibility_without_live_cancel_claim() -> None:
    provider = FakeProvider([ModelResponse(content="legacy answer")])
    events: list[ProviderEvent] = []

    result = LegacyProviderAdapter(provider).complete_request(
        make_request(), on_event=events.append
    )

    assert result.content == "legacy answer"
    assert result.request_id == "request-1"
    assert LegacyProviderAdapter(provider).supports_live_cancellation is False
    assert [event.type for event in events] == [
        ProviderEventType.REQUEST_STARTED,
        ProviderEventType.ATTEMPT_STARTED,
        ProviderEventType.RESPONSE_COMPLETED,
    ]
