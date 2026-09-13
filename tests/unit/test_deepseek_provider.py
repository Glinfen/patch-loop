from pathlib import Path
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


def completion_response_with_cache(
    *, cache_hit_tokens: int, cache_miss_tokens: int
) -> dict[str, Any]:
    response = completion_response()
    response["usage"].update(
        {
            "prompt_cache_hit_tokens": cache_hit_tokens,
            "prompt_cache_miss_tokens": cache_miss_tokens,
        }
    )
    return response


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
    assert response.usage.cache_hit_tokens is None
    assert response.usage.cache_miss_tokens is None
    assert response.usage.cost_usd == pytest.approx((12 * 0.14 + 5 * 0.28) / 1_000_000)
    url, headers, payload, _ = transport.requests[0]
    assert url == "https://api.deepseek.com/chat/completions"
    assert headers["Authorization"] == "Bearer test-secret"
    assert payload["model"] == "deepseek-flash"
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    assert payload["tools"][0]["function"]["description"].startswith("[read]")


def test_provider_preserves_cache_usage_and_prices_mixed_cache_tokens() -> None:
    provider = make_provider(
        FakeTransport([completion_response_with_cache(cache_hit_tokens=8, cache_miss_tokens=4)])
    )

    response = provider.complete([ModelMessage(role="user", content="Inspect")], [])

    assert response.usage.cache_hit_tokens == 8
    assert response.usage.cache_miss_tokens == 4
    assert response.usage.cost_usd == pytest.approx((8 * 0.0028 + 4 * 0.14 + 5 * 0.28) / 1_000_000)


@pytest.mark.parametrize(
    ("hit_tokens", "miss_tokens", "expected_input_cost"),
    [(12, 0, 12 * 0.0028), (0, 12, 12 * 0.14)],
)
def test_provider_prices_all_hit_and_all_miss_cache_usage(
    hit_tokens: int,
    miss_tokens: int,
    expected_input_cost: float,
) -> None:
    provider = make_provider(
        FakeTransport(
            [
                completion_response_with_cache(
                    cache_hit_tokens=hit_tokens,
                    cache_miss_tokens=miss_tokens,
                )
            ]
        )
    )

    response = provider.complete([ModelMessage(role="user", content="Inspect")], [])

    assert response.usage.cache_hit_tokens == hit_tokens
    assert response.usage.cache_miss_tokens == miss_tokens
    assert response.usage.cost_usd == pytest.approx((expected_input_cost + 5 * 0.28) / 1_000_000)


def test_provider_preserves_inconsistent_cache_usage_but_prices_conservatively() -> None:
    provider = make_provider(
        FakeTransport([completion_response_with_cache(cache_hit_tokens=7, cache_miss_tokens=4)])
    )

    response = provider.complete([ModelMessage(role="user", content="Inspect")], [])

    assert response.usage.cache_hit_tokens == 7
    assert response.usage.cache_miss_tokens == 4
    assert response.usage.cost_usd == pytest.approx((12 * 0.14 + 5 * 0.28) / 1_000_000)


def test_config_loads_generic_llm_names_from_explicit_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "DEEPSEEK_API_KEY",
        "LLM_API_KEY",
        "DEEPSEEK_BASE_URL",
        "LLM_BASE_URL",
        "DEEPSEEK_MODEL",
        "LLM_MODEL_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_API_KEY='file-secret'\n"
        "LLM_BASE_URL=https://example.test/v1\n"
        "LLM_MODEL_ID=deepseek-flash\n",
        encoding="utf-8",
    )

    config = DeepSeekConfig.from_env(env_file)

    assert config.api_key.get_secret_value() == "file-secret"
    assert config.base_url == "https://example.test/v1"
    assert config.model == "deepseek-flash"


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


def test_provider_accepts_legacy_flash_model() -> None:
    config = DeepSeekConfig(api_key=SecretStr("test-secret"), model="deepseek-v4-flash")

    provider = DeepSeekProvider(config)

    assert provider.name == "deepseek-v4-flash"


def test_provider_rejects_unsupported_model() -> None:
    config = DeepSeekConfig(api_key=SecretStr("test-secret"), model="deepseek-v4-pro")

    with pytest.raises(ValueError, match="deepseek-flash"):
        DeepSeekProvider(config)


def test_provider_redacts_key_from_transport_error() -> None:
    transport = FakeTransport([ProviderRequestError("failed with test-secret", retryable=False)])
    provider = make_provider(transport)

    with pytest.raises(RuntimeError, match=r"failed with \[REDACTED\]") as captured:
        provider.complete([ModelMessage(role="user", content="Inspect")], [])

    assert "test-secret" not in str(captured.value)
