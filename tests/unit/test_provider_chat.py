from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
    ToolSpec,
)
from patchloop.providers.chat import ChatStreamReducer
from patchloop.providers.sse import SSEDecoder, SSEFrame

FIXTURES = Path(__file__).parent.parent / "fixtures" / "providers" / "chat"


def make_binding(
    *,
    dialect: ChatDialect = ChatDialect.STANDARD,
    reasoning_enabled: bool = False,
    streaming: bool = True,
    structured_output: bool = True,
) -> ProviderBinding:
    return ProviderBinding(
        profile_id="chat-test",
        protocol=ProviderProtocol.CHAT_COMPLETIONS,
        dialect=dialect,
        model="custom-chat-model",
        base_url="https://api.example.test/v1",
        auth="none",
        capabilities=ProviderCapabilities(
            tools=True,
            multiple_tool_calls=True,
            streaming=True,
            reasoning_transport=(
                ReasoningTransport.DEEPSEEK_TEXT
                if dialect is ChatDialect.DEEPSEEK
                else ReasoningTransport.NONE
            ),
            structured_output=structured_output,
            context_window_tokens=8192,
            max_output_tokens=2048,
            usage_supported=True,
            cache_usage_supported=True,
        ),
        generation=ProviderGeneration(
            max_output_tokens=1024,
            token_limit_field="max_completion_tokens",
            temperature=0.25,
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
        request_id="request-chat-test",
        task_id="task-chat-test",
        step_index=0,
        purpose=ProviderRequestPurpose.AGENT_STEP,
        messages=messages or (ModelMessage(role="user", content="Inspect app.py"),),
        tools=tools,
        output_schema=output_schema,
    )


def test_standard_chat_encoding_preserves_tool_context_without_reasoning_fields() -> None:
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    messages = (
        ModelMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="call-old", name="read_file", arguments={"path": "a.py"})],
            continuation=ProviderContinuation(deepseek_reasoning_content="private thought"),
        ),
        ModelMessage(role="tool", content="contents", tool_call_id="call-old"),
    )
    tools = (
        ToolSpec(
            name="write_file",
            description="Write a file",
            permission="write",
            parameters=parameters,
        ),
    )
    encoded = ChatCompletionsAdapter().encode(
        make_request(messages=messages, tools=tools), make_binding()
    )

    assert encoded.body["model"] == "custom-chat-model"
    assert encoded.body["max_completion_tokens"] == 1024
    assert "max_tokens" not in encoded.body
    assert encoded.body["messages"][0]["tool_calls"][0]["id"] == "call-old"
    assert encoded.body["messages"][1]["tool_call_id"] == "call-old"
    assert "reasoning_content" not in encoded.body["messages"][0]
    assert encoded.body["tools"][0]["function"]["description"] == "[write] Write a file"
    assert encoded.body["tools"][0]["function"]["parameters"] == parameters
    assert "thinking" not in encoded.body
    assert "reasoning_effort" not in encoded.body
    assert encoded.stream is True


def test_empty_tools_are_omitted_and_structured_schema_is_sent_strictly() -> None:
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    encoded = ChatCompletionsAdapter().encode(
        make_request(output_schema=schema),
        make_binding(streaming=False),
    )

    assert "tools" not in encoded.body
    assert "tool_choice" not in encoded.body
    assert encoded.body["stream"] is False
    assert encoded.body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "patchloop_response", "strict": True, "schema": schema},
    }
    assert encoded.body["response_format"]["json_schema"]["schema"] == schema


def test_json_response_parses_tool_call_finish_usage_and_pricing() -> None:
    body = json.loads((FIXTURES / "completion.json").read_text(encoding="utf-8"))
    response = ChatCompletionsAdapter().parse_json(body, make_binding())

    assert response.tool_calls[0].id == "call-1"
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "app.py"}
    assert response.content == "Reading the file"
    assert response.finish_reason == "tool_calls"
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 4
    assert response.usage.input_tokens_reported is True
    assert response.usage.cost_usd == pytest.approx((10 + 4 * 2) / 1_000_000)


def test_json_response_requires_finish_reason_and_surfaces_refusal() -> None:
    adapter = ChatCompletionsAdapter()
    binding = make_binding()
    with pytest.raises(ProviderError) as missing_finish:
        adapter.parse_json(
            {"choices": [{"message": {"content": "answer"}}]},
            binding,
        )
    assert missing_finish.value.kind is ProviderErrorKind.PROTOCOL

    with pytest.raises(ProviderError) as refusal:
        adapter.parse_json(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "", "refusal": "cannot comply"},
                    }
                ]
            },
            binding,
        )
    assert refusal.value.kind is ProviderErrorKind.REFUSAL

    with pytest.raises(ProviderError) as filtered:
        adapter.parse_json(
            {
                "choices": [
                    {
                        "finish_reason": "content_filter",
                        "message": {"content": ""},
                    }
                ]
            },
            binding,
        )
    assert filtered.value.kind is ProviderErrorKind.REFUSAL

    with pytest.raises(ProviderError) as resource_error:
        adapter.parse_json(
            {
                "choices": [
                    {
                        "finish_reason": "resource_exhausted",
                        "message": {"content": "answer"},
                    }
                ]
            },
            binding,
        )
    assert resource_error.value.kind is ProviderErrorKind.CONNECTION


def test_deepseek_adapter_sends_and_replays_reasoning_continuation_without_tools() -> None:
    binding = make_binding(dialect=ChatDialect.DEEPSEEK, reasoning_enabled=True)
    messages = (
        ModelMessage(role="user", content="Think"),
        ModelMessage(
            role="assistant",
            content="Here is the answer",
            continuation=ProviderContinuation(deepseek_reasoning_content="private reasoning"),
        ),
    )
    encoded = ChatCompletionsAdapter(ChatDialect.DEEPSEEK).encode(
        make_request(messages=messages), binding
    )

    assert encoded.body["thinking"] == {"type": "enabled"}
    assert encoded.body["reasoning_effort"] == "medium"
    assert encoded.body["messages"][1]["reasoning_content"] == "private reasoning"
    assert "tools" not in encoded.body
    assert "tool_choice" not in encoded.body

    response = ChatCompletionsAdapter(ChatDialect.DEEPSEEK).parse_json(
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": "done",
                        "reasoning_content": "new private reasoning",
                    },
                }
            ]
        },
        binding,
    )
    assert response.continuation == ProviderContinuation(
        deepseek_reasoning_content="new private reasoning"
    )


def test_deepseek_enabled_reasoning_requires_replayable_history_and_response() -> None:
    adapter = ChatCompletionsAdapter(ChatDialect.DEEPSEEK)
    binding = make_binding(dialect=ChatDialect.DEEPSEEK, reasoning_enabled=True)
    with pytest.raises(ProviderError) as missing_history:
        adapter.encode(
            make_request(messages=(ModelMessage(role="assistant", content="old answer"),)),
            binding,
        )
    assert missing_history.value.kind is ProviderErrorKind.CONTINUATION_UNAVAILABLE

    with pytest.raises(ProviderError) as missing_response:
        adapter.parse_json(
            {"choices": [{"finish_reason": "stop", "message": {"content": "answer"}}]},
            binding,
        )
    assert missing_response.value.kind is ProviderErrorKind.CONTINUATION_UNAVAILABLE


def test_chat_stream_reducer_aggregates_tool_fragments_and_usage_only_tail() -> None:
    binding = make_binding()
    reducer = ChatStreamReducer(ChatDialect.STANDARD, binding)
    decoder = SSEDecoder()
    events = []
    raw = (FIXTURES / "stream.sse").read_bytes()
    frames = decoder.feed(raw) + decoder.finish()
    for frame in frames:
        events.extend(reducer.feed(frame))
    response = reducer.finish()

    assert response.content == "Reading the file"
    assert response.tool_calls[0].id == "call-1"
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "app.py"}
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 4
    assert any(event.type.value == "usage" for event in events)
    json_response = ChatCompletionsAdapter().parse_json(
        json.loads((FIXTURES / "completion.json").read_text(encoding="utf-8")), binding
    )
    assert response == json_response


def test_chat_stream_requires_finish_and_done_and_allows_unknown_usage() -> None:
    binding = make_binding()
    reducer = ChatStreamReducer(ChatDialect.STANDARD, binding)
    reducer.feed(SSEFrame(None, '{"choices":[{"index":0,"delta":{"content":"partial"}}]}', None))
    with pytest.raises(ProviderError) as missing_terminal:
        reducer.finish()
    assert missing_terminal.value.kind is ProviderErrorKind.TRUNCATED

    reducer = ChatStreamReducer(ChatDialect.STANDARD, binding)
    reducer.feed(
        SSEFrame(
            None,
            '{"choices":[{"index":0,"delta":{"content":"done"},"finish_reason":"stop"}]}',
            None,
        )
    )
    reducer.feed(SSEFrame(None, "[DONE]", None))
    response = reducer.finish()
    assert response.content == "done"
    assert response.usage.cost_status == "unknown"
    assert response.usage.input_tokens_reported is False


def test_deepseek_stream_stores_reasoning_continuation() -> None:
    binding = make_binding(dialect=ChatDialect.DEEPSEEK, reasoning_enabled=True)
    reducer = ChatStreamReducer(ChatDialect.DEEPSEEK, binding)
    for data in (
        '{"choices":[{"index":0,"delta":{"content":"answer","reasoning_content":"thought"},"finish_reason":null}]}',
        '{"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        "[DONE]",
    ):
        reducer.feed(SSEFrame(None, data, None))

    assert reducer.finish().continuation == ProviderContinuation(
        deepseek_reasoning_content="thought"
    )


def test_stream_reducer_aggregates_interleaved_tool_calls_by_index() -> None:
    reducer = ChatStreamReducer(ChatDialect.STANDARD, make_binding())
    frames = (
        '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":1,"id":"call-1","function":{"name":"second_","arguments":"{\\"value\\":"}}]}}]}',
        '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call-0","function":{"name":"first","arguments":"{\\"value\\":0}"}}]}}]}',
        '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":1,"function":{"name":"call","arguments":"1}"}}]},"finish_reason":null}]}',
        '{"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
        "[DONE]",
    )
    for data in frames:
        reducer.feed(SSEFrame(None, data, None))

    response = reducer.finish()
    assert [(call.id, call.name, call.arguments) for call in response.tool_calls] == [
        ("call-0", "first", {"value": 0}),
        ("call-1", "second_call", {"value": 1}),
    ]


def test_stream_reducer_rejects_changed_tool_call_ids() -> None:
    reducer = ChatStreamReducer(ChatDialect.STANDARD, make_binding())
    reducer.feed(
        SSEFrame(
            None,
            '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call-a","function":{"name":"read_file"}}]}}]}',
            None,
        )
    )
    with pytest.raises(ProviderError) as changed_id:
        reducer.feed(
            SSEFrame(
                None,
                '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call-b"}]}}]}',
                None,
            )
        )
    assert changed_id.value.kind is ProviderErrorKind.PROTOCOL


def test_stream_reducer_rejects_tool_call_without_id() -> None:
    reducer = ChatStreamReducer(ChatDialect.STANDARD, make_binding())
    reducer.feed(
        SSEFrame(
            None,
            '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"name":"read_file"}}]}}]}',
            None,
        )
    )
    reducer.feed(
        SSEFrame(None, '{"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}', None)
    )
    reducer.feed(SSEFrame(None, "[DONE]", None))
    with pytest.raises(ProviderError) as missing_id:
        reducer.finish()
    assert missing_id.value.kind is ProviderErrorKind.PROTOCOL


def test_deepseek_disabled_thinking_is_explicit() -> None:
    binding = make_binding(dialect=ChatDialect.DEEPSEEK, reasoning_enabled=False)
    encoded = ChatCompletionsAdapter(ChatDialect.DEEPSEEK).encode(make_request(), binding)

    assert encoded.body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in encoded.body
