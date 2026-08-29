from typing import Any

import pytest
from pydantic import SecretStr

from patchloop.domain import ToolCall
from patchloop.providers import DeepSeekConfig, DeepSeekProvider, ModelMessage, ToolSpec
from patchloop.providers.deepseek import ProviderRequestError


class FakeTransport:
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self.responses = iter(responses)
        self.requests: list[tuple[str, dict[str, str], dict[str, Any], float]] = []

    def post(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.requests.append((url, headers, payload, timeout_seconds))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def completion_response(arguments: str = '{"path":"app.py"}') -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": arguments},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5},
    }


def make_provider(transport: FakeTransport) -> DeepSeekProvider:
    config = DeepSeekConfig(api_key=SecretStr("test-secret"), max_retries=0)
    return DeepSeekProvider(config, transport=transport)


def test_provider_maps_messages_tools_and_usage() -> None:
    transport = FakeTransport([completion_response()])
    provider = make_provider(transport)
    tools = [
        ToolSpec(
            name="read_file",
            description="Read a file",
            parameters={"type": "object"},
            permission="read",
        )
    ]

    response = provider.complete([ModelMessage(role="user", content="Inspect app.py")], tools)

    assert response.tool_calls[0].arguments == {"path": "app.py"}
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 5
    assert response.usage.cost_usd == pytest.approx((12 * 0.14 + 5 * 0.28) / 1_000_000)
    url, headers, payload, _ = transport.requests[0]
    assert url == "https://api.deepseek.com/chat/completions"
    assert headers["Authorization"] == "Bearer test-secret"
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    assert payload["tools"][0]["function"]["description"].startswith("[read]")


def test_provider_preserves_tool_call_context() -> None:
    transport = FakeTransport([completion_response()])
    provider = make_provider(transport)
    prior_messages = [
        ModelMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="call-previous", name="read_file", arguments={})],
        ),
        ModelMessage(role="tool", content="result", tool_call_id="call-previous"),
    ]

    provider.complete(prior_messages, [])

    messages = transport.requests[0][2]["messages"]
    assert messages[0]["tool_calls"][0]["id"] == "call-previous"
    assert messages[1]["tool_call_id"] == "call-previous"


def test_provider_marks_invalid_tool_json() -> None:
    provider = make_provider(FakeTransport([completion_response("{not-json")]))

    response = provider.complete([ModelMessage(role="user", content="Inspect")], [])

    assert response.tool_calls[0].arguments == {}
    assert (
        response.tool_calls[0].arguments_error
        == "invalid tool arguments JSON: Expecting property name enclosed in double quotes"
    )


def test_provider_retries_transient_failure_without_leaking_key() -> None:
    delays: list[float] = []
    transport = FakeTransport(
        [
            ProviderRequestError("temporary", retryable=True),
            completion_response(),
        ]
    )
    config = DeepSeekConfig(api_key=SecretStr("test-secret"), max_retries=1)
    provider = DeepSeekProvider(config, transport=transport, sleeper=delays.append)

    provider.complete([ModelMessage(role="user", content="Inspect")], [])

    assert delays == [1.0]
    assert len(transport.requests) == 2
    assert "test-secret" not in repr(config)


def test_provider_rejects_non_flash_model() -> None:
    config = DeepSeekConfig(api_key=SecretStr("test-secret"), model="deepseek-v4-pro")

    with pytest.raises(ValueError, match="deepseek-v4-flash"):
        DeepSeekProvider(config)


def test_provider_redacts_key_from_transport_error() -> None:
    transport = FakeTransport([ProviderRequestError("failed with test-secret", retryable=False)])
    provider = make_provider(transport)

    with pytest.raises(RuntimeError, match=r"failed with \[REDACTED\]") as captured:
        provider.complete([ModelMessage(role="user", content="Inspect")], [])

    assert "test-secret" not in str(captured.value)
