import json

import pytest
from pydantic import ValidationError

from patchloop.providers import (
    ChatDialect,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    ProviderAuth,
    ProviderBinding,
    ProviderCapabilities,
    ProviderContinuation,
    ProviderError,
    ProviderEvent,
    ProviderGeneration,
    ProviderPricing,
    ProviderProtocol,
    ProviderRequest,
    ProviderRequestPurpose,
    ReasoningTransport,
    ToolSpec,
)


def binding(**changes: object) -> ProviderBinding:
    values: dict[str, object] = {
        "profile_id": "deepseek",
        "protocol": ProviderProtocol.CHAT_COMPLETIONS,
        "dialect": ChatDialect.DEEPSEEK,
        "model": "configured-model",
        "base_url": "https://api.example.test/v1",
        "auth": ProviderAuth.BEARER,
        "credential_env": "EXAMPLE_API_KEY",
        "capabilities": ProviderCapabilities(
            tools=True,
            multiple_tool_calls=True,
            streaming=True,
            reasoning_transport=ReasoningTransport.DEEPSEEK_TEXT,
            structured_output=True,
            context_window_tokens=32_000,
            max_output_tokens=4_096,
            usage_supported=True,
            cache_usage_supported=True,
        ),
        "generation": ProviderGeneration(
            max_output_tokens=2_048,
            reasoning_enabled=True,
            reasoning_effort="high",
        ),
        "pricing": ProviderPricing(
            version="test-v1",
            input_per_million=0.1,
            output_per_million=0.2,
        ),
    }
    values.update(changes)
    return ProviderBinding.model_validate(values)


def test_legacy_message_response_and_usage_fixtures_remain_readable() -> None:
    message = ModelMessage.model_validate({"role": "user", "content": "hello"})
    response = ModelResponse.model_validate(
        {"content": "done", "usage": {"input_tokens": 3, "output_tokens": 1}}
    )

    assert message.continuation is None
    assert response.request_id is None
    assert response.finish_reason is None
    assert response.usage.cost_status == "legacy"
    assert response.usage.input_tokens_reported is None


def test_binding_fingerprint_is_stable_and_covers_request_configuration() -> None:
    first = binding()
    restored = ProviderBinding.model_validate_json(first.model_dump_json())
    changed = binding(model="another-configured-model")

    assert restored == first
    assert len(first.fingerprint) == 64
    assert changed.fingerprint != first.fingerprint


def test_binding_contains_no_credential_value_in_dump_or_repr() -> None:
    selected = binding()
    dumped = json.dumps(selected.model_dump(mode="json"))

    assert "actual-secret" not in dumped
    assert "actual-secret" not in repr(selected)
    assert "EXAMPLE_API_KEY" in dumped


def test_binding_rejects_tampered_fingerprint() -> None:
    payload = binding().model_dump(mode="json")
    payload["model"] = "tampered"

    with pytest.raises(ValidationError, match="fingerprint"):
        ProviderBinding.model_validate(payload)


@pytest.mark.parametrize(
    "capabilities",
    [
        ProviderCapabilities.model_construct(
            tools=False,
            multiple_tool_calls=True,
            streaming=False,
            reasoning_transport=ReasoningTransport.NONE,
            structured_output=False,
            context_window_tokens=100,
            max_output_tokens=10,
            usage_supported=False,
            cache_usage_supported=False,
        ),
        ProviderCapabilities.model_construct(
            tools=False,
            multiple_tool_calls=False,
            streaming=False,
            reasoning_transport=ReasoningTransport.NONE,
            structured_output=False,
            context_window_tokens=100,
            max_output_tokens=10,
            usage_supported=False,
            cache_usage_supported=True,
        ),
    ],
)
def test_capability_combinations_are_validated_on_binding(
    capabilities: ProviderCapabilities,
) -> None:
    with pytest.raises(ValidationError):
        binding(capabilities=capabilities.model_dump())


def test_generation_cannot_exceed_model_capability() -> None:
    with pytest.raises(ValidationError, match="exceeds"):
        binding(generation=ProviderGeneration(max_output_tokens=8_192))


def test_request_and_response_round_trip_with_continuation() -> None:
    continuation = ProviderContinuation(deepseek_reasoning_content="opaque reasoning")
    request = ProviderRequest(
        request_id="task-1:agent_step:0:0:0",
        task_id="task-1",
        step_index=0,
        purpose=ProviderRequestPurpose.AGENT_STEP,
        messages=(ModelMessage(role="assistant", content="", continuation=continuation),),
        tools=(ToolSpec(name="read_file", description="Read", parameters={}),),
        max_output_tokens=128,
    )
    response = ModelResponse(
        content="done",
        continuation=continuation,
        request_id=request.request_id,
        finish_reason="stop",
        usage=ModelUsage(
            input_tokens=4,
            output_tokens=1,
            input_tokens_reported=True,
            output_tokens_reported=True,
            cost_status="estimated",
            pricing_version="test-v1",
        ),
    )

    assert ProviderRequest.model_validate_json(request.model_dump_json()) == request
    assert ModelResponse.model_validate_json(response.model_dump_json()) == response


def test_unknown_fields_events_and_error_kinds_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ProviderRequest.model_validate(
            {
                "request_id": "request-1",
                "task_id": "task-1",
                "step_index": 0,
                "purpose": "agent_step",
                "messages": [],
                "surprise": True,
            }
        )
    with pytest.raises(ValidationError):
        ProviderEvent.model_validate({"type": "unknown", "request_id": "request-1", "sequence": 0})
    with pytest.raises(ValueError):
        ProviderError("unknown", "safe")
