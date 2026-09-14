from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar

import pytest

from patchloop.domain import ToolCall
from patchloop.providers import (
    ChatCompletionsAdapter,
    ChatDialect,
    ModelMessage,
    ProviderBinding,
    ProviderCapabilities,
    ProviderContinuation,
    ProviderError,
    ProviderErrorKind,
    ProviderGeneration,
    ProviderPricing,
    ProviderProtocol,
    ProviderRequest,
    ProviderRequestPurpose,
    ProviderTransportConfig,
    ReasoningTransport,
    ResponsesAdapter,
    ToolSpec,
    ValidatedResponseItem,
)
from patchloop.providers.base import EncodedRequest
from patchloop.providers.gateway import ProviderGateway
from patchloop.providers.responses import ResponsesStreamReducer
from patchloop.providers.sse import SSEDecoder, SSEFrame

FIXTURES = Path(__file__).parent.parent / "fixtures" / "providers" / "responses"


def make_binding(
    *,
    streaming: bool = True,
    reasoning_enabled: bool = True,
    temperature: float | None = 0.25,
    multiple_tool_calls: bool = True,
) -> ProviderBinding:
    return ProviderBinding(
        profile_id="responses-test",
        protocol=ProviderProtocol.RESPONSES,
        model="custom-responses-model",
        base_url="https://api.example.test/v1",
        auth="none",
        capabilities=ProviderCapabilities(
            tools=True,
            multiple_tool_calls=multiple_tool_calls,
            streaming=True,
            reasoning_transport=ReasoningTransport.RESPONSES_ITEMS,
            structured_output=True,
            context_window_tokens=8192,
            max_output_tokens=2048,
            usage_supported=True,
            cache_usage_supported=True,
        ),
        generation=ProviderGeneration(
            max_output_tokens=1024,
            temperature=temperature,
            reasoning_enabled=reasoning_enabled,
            reasoning_effort="medium" if reasoning_enabled else None,
        ),
        transport=ProviderTransportConfig(streaming=streaming),
        pricing=ProviderPricing(
            version="test-pricing-v1",
            input_per_million=1.0,
            output_per_million=2.0,
            cached_input_per_million=0.5,
        ),
    )


def make_request(
    *,
    messages: tuple[ModelMessage, ...] | None = None,
    tools: tuple[ToolSpec, ...] = (),
    output_schema: dict[str, Any] | None = None,
) -> ProviderRequest:
    return ProviderRequest(
        request_id="request-responses-test",
        task_id="task-responses-test",
        step_index=0,
        purpose=ProviderRequestPurpose.AGENT_STEP,
        messages=messages or (ModelMessage(role="user", content="Inspect a.py"),),
        tools=tools,
        output_schema=output_schema,
    )


def completion_fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "completion.json").read_text(encoding="utf-8"))


def test_responses_encoding_preserves_roles_and_pairs_function_output_by_call_id() -> None:
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    tools = (ToolSpec(name="read_file", description="Read source", parameters=parameters),)
    messages = (
        ModelMessage(role="system", content="Follow project policy"),
        ModelMessage(role="developer", content="Inspect before editing"),
        ModelMessage(role="user", content="Read a.py"),
        ModelMessage(
            role="assistant",
            content="I will inspect it.",
            tool_calls=[ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"})],
        ),
        ModelMessage(role="tool", tool_call_id="call_1", content="file contents"),
    )

    encoded = ResponsesAdapter().encode(
        make_request(messages=messages, tools=tools), make_binding()
    )

    assert encoded.path == "/responses"
    assert encoded.body["input"] == [
        {"type": "message", "role": "system", "content": "Follow project policy"},
        {"type": "message", "role": "developer", "content": "Inspect before editing"},
        {"type": "message", "role": "user", "content": "Read a.py"},
        {"type": "message", "role": "assistant", "content": "I will inspect it."},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": '{"path":"a.py"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "file contents"},
    ]
    assert encoded.body["store"] is False
    assert "previous_response_id" not in encoded.body
    assert encoded.body["tools"] == [
        {
            "type": "function",
            "name": "read_file",
            "description": "Read source",
            "parameters": parameters,
            "strict": False,
        }
    ]
    assert encoded.body["include"] == ["reasoning.encrypted_content"]
    assert encoded.body["reasoning"] == {"effort": "medium"}


def test_responses_encoding_only_sends_configured_optional_fields_and_parallel_limit() -> None:
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    request = make_request(
        tools=(ToolSpec(name="inspect", description="Inspect", parameters={"type": "object"}),),
        output_schema=schema,
    )
    binding = make_binding(
        streaming=False,
        reasoning_enabled=False,
        temperature=None,
        multiple_tool_calls=False,
    )
    encoded = ResponsesAdapter().encode(request, binding)

    assert encoded.stream is False
    assert encoded.body["parallel_tool_calls"] is False
    assert encoded.body["text"] == {
        "format": {
            "type": "json_schema",
            "name": "patchloop_response",
            "schema": schema,
            "strict": True,
        }
    }
    for field in ("temperature", "reasoning", "include"):
        assert field not in encoded.body
    assert encoded.body["max_output_tokens"] == 1024


def test_same_tool_spec_uses_distinct_chat_and_responses_wire_shapes() -> None:
    tool = ToolSpec(
        name="inspect",
        description="Inspect source",
        permission="read",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    request = make_request(tools=(tool,))
    response_wire = ResponsesAdapter().encode(request, make_binding()).body["tools"][0]
    chat_binding = ProviderBinding(
        profile_id="chat-wire-test",
        protocol=ProviderProtocol.CHAT_COMPLETIONS,
        dialect=ChatDialect.STANDARD,
        model="custom-chat-model",
        base_url="https://api.example.test/v1",
        auth="none",
        capabilities=ProviderCapabilities(
            tools=True,
            streaming=True,
            structured_output=True,
            context_window_tokens=8192,
            max_output_tokens=2048,
        ),
        generation=ProviderGeneration(max_output_tokens=1024),
        transport=ProviderTransportConfig(streaming=False),
    )
    chat_wire = ChatCompletionsAdapter().encode(request, chat_binding).body["tools"][0]

    assert response_wire == {
        "type": "function",
        "name": "inspect",
        "description": "Inspect source",
        "parameters": tool.parameters,
        "strict": False,
    }
    assert chat_wire["type"] == "function"
    assert chat_wire["function"]["name"] == response_wire["name"]
    assert chat_wire["function"]["parameters"] == response_wire["parameters"]


def test_json_response_keeps_ordered_continuation_and_uses_call_id_not_output_id() -> None:
    response = ResponsesAdapter().parse_json(completion_fixture(), make_binding())

    assert response.content == "Ready"
    assert response.tool_calls[0].id == "call_1"
    assert response.tool_calls[0].id != "fc_item_1"
    assert response.tool_calls[0].arguments == {"path": "a.py"}
    assert response.continuation is not None
    assert [item.type for item in response.continuation.responses_items] == [
        "message",
        "reasoning",
        "function_call",
    ]
    assert (
        response.continuation.responses_items[1].item["encrypted_content"]
        == "opaque-encrypted-reasoning"
    )
    assert response.request_id is None
    assert response.usage.input_tokens == 20
    assert response.usage.output_tokens == 5
    assert response.usage.cache_hit_tokens == 3
    assert response.usage.cache_miss_tokens == 17
    assert response.usage.cache_write_tokens == 2
    assert response.usage.cost_usd == pytest.approx(28.5 / 1_000_000)


def test_responses_continuation_replays_native_items_without_duplicate_normalized_content() -> None:
    response = ResponsesAdapter().parse_json(completion_fixture(), make_binding())
    assert response.continuation is not None
    history = (
        ModelMessage(
            role="assistant",
            content=response.content,
            tool_calls=response.tool_calls,
            continuation=response.continuation,
        ),
        ModelMessage(role="tool", tool_call_id="call_1", content="file contents"),
    )

    encoded = ResponsesAdapter().encode(make_request(messages=history), make_binding())

    assert [item["type"] for item in encoded.body["input"]] == [
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert encoded.body["input"][0]["content"] == [
        {"type": "output_text", "text": "Ready", "annotations": []}
    ]
    assert encoded.body["input"][1]["encrypted_content"] == "opaque-encrypted-reasoning"
    assert encoded.body["input"][2]["id"] == "fc_item_1"
    assert encoded.body["input"][2]["call_id"] == "call_1"
    assert encoded.body["input"][3]["call_id"] == "call_1"
    assert sum(item.get("content") == "Ready" for item in encoded.body["input"]) == 0


def test_responses_keeps_invalid_function_arguments_as_an_explicit_tool_error() -> None:
    body = completion_fixture()
    body["output"][2]["arguments"] = "[not-an-object]"

    response = ResponsesAdapter().parse_json(body, make_binding())

    assert response.tool_calls[0].arguments == {}
    assert response.tool_calls[0].arguments_error is not None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("incomplete", ProviderErrorKind.TRUNCATED),
        ("failed", ProviderErrorKind.CONNECTION),
        ("in_progress", ProviderErrorKind.PROTOCOL),
    ],
)
def test_json_response_requires_completed_status(status: str, expected: ProviderErrorKind) -> None:
    body = completion_fixture()
    body["status"] = status

    with pytest.raises(ProviderError) as error:
        ResponsesAdapter().parse_json(body, make_binding())

    assert error.value.kind is expected


def test_responses_refusal_and_hosted_tools_are_structured_errors() -> None:
    adapter = ResponsesAdapter()
    refusal = completion_fixture()
    refusal["output"] = [
        {
            "id": "msg_refusal",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "refusal", "refusal": "cannot comply"}],
        }
    ]
    with pytest.raises(ProviderError) as refusal_error:
        adapter.parse_json(refusal, make_binding())
    assert refusal_error.value.kind is ProviderErrorKind.REFUSAL

    hosted = completion_fixture()
    hosted["output"] = [{"id": "web_1", "type": "web_search_call", "status": "completed"}]
    with pytest.raises(ProviderError) as hosted_error:
        adapter.parse_json(hosted, make_binding())
    assert hosted_error.value.kind is ProviderErrorKind.UNSUPPORTED_OUTPUT


def test_responses_stream_matches_json_and_emits_text_tool_and_reasoning_deltas() -> None:
    binding = make_binding()
    reducer = ResponsesStreamReducer(binding)
    decoder = SSEDecoder()
    events = []
    raw = (FIXTURES / "stream.sse").read_bytes()
    for frame in decoder.feed(raw) + decoder.finish():
        events.extend(reducer.feed(frame))

    streamed = reducer.finish()
    parsed = ResponsesAdapter().parse_json(completion_fixture(), binding)

    assert streamed == parsed
    assert [event.type.value for event in events] == [
        "text_delta",
        "reasoning_delta",
        "tool_call_delta",
        "tool_call_delta",
    ]
    assert [event.tool_call_index for event in events[-2:]] == [0, 0]


def test_responses_stream_requires_terminal_completion_and_cross_checks_deltas() -> None:
    raw = (FIXTURES / "stream.sse").read_bytes()
    partial = raw.split(b"event: response.completed", maxsplit=1)[0]
    decoder = SSEDecoder()
    reducer = ResponsesStreamReducer(make_binding())
    for frame in decoder.feed(partial) + decoder.finish():
        reducer.feed(frame)
    with pytest.raises(ProviderError) as missing_terminal:
        reducer.finish()
    assert missing_terminal.value.kind is ProviderErrorKind.TRUNCATED

    reducer = ResponsesStreamReducer(make_binding())
    frames = (
        SSEFrame(
            "response.output_item.added",
            '{"type":"response.output_item.added","output_index":0,"item":{"id":"msg_1","type":"message","status":"in_progress","role":"assistant","content":[]}}',
            None,
        ),
        SSEFrame(
            "response.output_text.delta",
            '{"type":"response.output_text.delta","item_id":"msg_1","output_index":0,"content_index":0,"delta":"wrong"}',
            None,
        ),
        SSEFrame(
            "response.output_item.done",
            '{"type":"response.output_item.done","output_index":0,"item":{"id":"msg_1","type":"message","status":"completed","role":"assistant","content":[{"type":"output_text","text":"right"}]}}',
            None,
        ),
    )
    reducer.feed(frames[0])
    reducer.feed(frames[1])
    with pytest.raises(ProviderError) as mismatch:
        reducer.feed(frames[2])
    assert mismatch.value.kind is ProviderErrorKind.PROTOCOL


@pytest.mark.parametrize(
    ("event_type", "status", "expected"),
    [
        ("response.incomplete", "incomplete", ProviderErrorKind.TRUNCATED),
        ("response.failed", "failed", ProviderErrorKind.CONNECTION),
    ],
)
def test_responses_stream_rejects_failed_and_incomplete_terminal_events(
    event_type: str,
    status: str,
    expected: ProviderErrorKind,
) -> None:
    reducer = ResponsesStreamReducer(make_binding())
    payload = json.dumps({"type": event_type, "response": {"status": status}})

    with pytest.raises(ProviderError) as error:
        reducer.feed(SSEFrame(event_type, payload, None))

    assert error.value.kind is expected


class _JsonResponse:
    status_code = 200
    headers: ClassVar[dict[str, str]] = {"content-type": "application/json"}

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = json.dumps(body).encode()

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        yield self.body

    async def aclose(self) -> None:
        return None


class _JsonTransport:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body

    @asynccontextmanager
    async def client_scope(self) -> AsyncIterator[None]:
        yield

    @asynccontextmanager
    async def open(
        self,
        encoded: EncodedRequest,
        timeouts: ProviderTransportConfig,
        *,
        control: Any = None,
    ) -> AsyncIterator[_JsonResponse]:
        del encoded, timeouts, control
        yield _JsonResponse(self.body)


def test_gateway_still_applies_local_schema_validation_to_responses_output() -> None:
    body = completion_fixture()
    body["output"] = [
        {
            "id": "msg_1",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": '{"ok":"not-a-boolean"}',
                    "annotations": [],
                }
            ],
        }
    ]
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    gateway = ProviderGateway(
        make_binding(streaming=False, reasoning_enabled=False),
        ResponsesAdapter(),
        _JsonTransport(body),  # type: ignore[arg-type]
        sleep=asyncio.sleep,
    )

    with pytest.raises(ProviderError) as error:
        gateway.complete_request(make_request(output_schema=schema))

    assert error.value.kind is ProviderErrorKind.STRUCTURED_OUTPUT_INVALID


def test_responses_rejects_invalid_stored_continuation_type() -> None:
    item = ValidatedResponseItem(
        type="message",
        item={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "ready"}],
        },
    )
    message = ModelMessage(
        role="assistant",
        content="ignored",
        continuation=ProviderContinuation(responses_items=(item,)),
    )
    item.item["type"] = "function_call"
    item.item.update({"call_id": "call_1", "name": "read_file", "arguments": "{}"})

    with pytest.raises(ProviderError) as error:
        ResponsesAdapter().encode(make_request(messages=(message,)), make_binding())

    assert error.value.kind is ProviderErrorKind.CONTINUATION_UNAVAILABLE
