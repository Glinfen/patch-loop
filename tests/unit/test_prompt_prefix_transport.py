"""Compare actual Gateway transport payloads, including restored native continuation."""

import copy
from contextlib import asynccontextmanager

import pytest

from patchloop.domain import ToolCall
from patchloop.providers import (
    ChatCompletionsAdapter,
    ChatDialect,
    ModelMessage,
    ProviderContinuation,
    ProviderRequest,
    ProviderRequestPurpose,
    ResponsesAdapter,
    ToolSpec,
    ValidatedResponseItem,
)
from patchloop.providers.gateway import ProviderGateway
from tests.unit.test_provider_chat import make_binding as chat_binding
from tests.unit.test_provider_responses import _JsonResponse
from tests.unit.test_provider_responses import make_binding as responses_binding


@pytest.mark.parametrize("responses", [False, True])
def test_compression_keeps_tools_but_disables_calls(responses):
    binding = responses_binding(streaming=False) if responses else chat_binding(streaming=False)
    adapter = ResponsesAdapter() if responses else ChatCompletionsAdapter()
    request = ProviderRequest(
        request_id="source",
        task_id="task",
        step_index=0,
        purpose="agent_step",
        messages=(ModelMessage(role="user", content="Keep this exact prefix."),),
        tools=(ToolSpec(name="read_file", description="read", parameters={"type": "object"}),),
    )
    source = adapter.encode(request, binding).body
    compressed = request.model_copy(
        update={
            "request_id": "compression",
            "purpose": ProviderRequestPurpose.EPOCH_COMPRESSION,
            "messages": (
                *request.messages,
                ModelMessage(role="user", content="Return summary JSON."),
            ),
        }
    )
    body = adapter.encode(compressed, binding).body
    assert body["tools"] == source["tools"]
    key = "input" if responses else "messages"
    assert body[key][: len(source[key])] == source[key]
    assert body["tool_choice"] == "none"
    assert source.get("tool_choice", "auto") == "auto"


class _CaptureTransport:
    def __init__(self, responses: bool):
        self.payloads = []
        self.body = (
            {
                "id": "resp1",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "id": "message1",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
            }
            if responses
            else {
                "id": "chat1",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "done",
                            "reasoning_content": "done",
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    @asynccontextmanager
    async def client_scope(self):
        yield

    @asynccontextmanager
    async def open(self, encoded, timeouts, *, control=None):
        self.payloads.append(copy.deepcopy(encoded.body))
        yield _JsonResponse(self.body)


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("corrupt", [False, True])
def test_final_payload_keeps_prior_items_after_checkpoint_roundtrip(protocol, corrupt):
    responses = protocol == "responses"
    binding = (
        responses_binding(streaming=False)
        if responses
        else chat_binding(
            dialect=ChatDialect.DEEPSEEK,
            reasoning_enabled=True,
            streaming=False,
        )
    )
    adapter = ResponsesAdapter() if responses else ChatCompletionsAdapter(ChatDialect.DEEPSEEK)
    continuation = (
        ProviderContinuation(
            responses_items=(
                ValidatedResponseItem(
                    type="reasoning",
                    item={
                        "id": "reason1",
                        "type": "reasoning",
                        "summary": [],
                        "encrypted_content": "opaque-encrypted-content",
                    },
                ),
                ValidatedResponseItem(
                    type="function_call",
                    item={
                        "id": "item1",
                        "type": "function_call",
                        "call_id": "call1",
                        "name": "read_file",
                        "arguments": '{"z": 1, "a": "路径"}',
                    },
                ),
            )
        )
        if responses
        else ProviderContinuation(deepseek_reasoning_content="Exact reasoning.\n")
    )
    request = ProviderRequest(
        request_id="first",
        task_id="task1",
        step_index=0,
        purpose="agent_step",
        messages=(
            ModelMessage(role="system", content="Fixed system."),
            ModelMessage(role="user", content="Read the repository."),
            ModelMessage(
                role="assistant",
                content="",
                continuation=continuation,
                tool_calls=[
                    ToolCall(id="call1", name="read_file", arguments={"z": 1, "a": "路径"}),
                ],
            ),
            ModelMessage(role="tool", content='{"output": "first result"}', tool_call_id="call1"),
        ),
        tools=tuple(
            ToolSpec(name=name, description=name, parameters={"type": "object"})
            for name in ("zeta", "read_file")
        ),
    )
    original_encode = adapter.encode
    if corrupt:

        def bad_encode(request, binding):
            encoded = original_encode(request, binding)
            if request.step_index == 1:
                encoded.body["input" if responses else "messages"][0]["content"] = "rewritten"
            return encoded

        adapter.encode = bad_encode
    transport = _CaptureTransport(responses)
    gateway = ProviderGateway(binding, adapter, transport)
    gateway.complete_request(request)
    restored = ProviderRequest.model_validate_json(request.model_dump_json())
    extended = restored.model_copy(
        update={
            "request_id": "second",
            "step_index": 1,
            "messages": (
                *restored.messages,
                ModelMessage(role="user", content="Next instruction."),
            ),
        }
    )
    gateway.complete_request(extended)
    first, second = transport.payloads
    key = "input" if responses else "messages"

    def assert_prefix():
        assert first[key] == second[key][: len(first[key])]
        assert first["tools"] == second["tools"]

    if corrupt:
        with pytest.raises(AssertionError):
            assert_prefix()
    else:
        assert_prefix()
        if responses:
            assert first[key][2]["encrypted_content"] == "opaque-encrypted-content"
            assert first[key][3]["arguments"] == '{"z": 1, "a": "路径"}'
        else:
            assert first[key][2]["reasoning_content"] == "Exact reasoning.\n"
