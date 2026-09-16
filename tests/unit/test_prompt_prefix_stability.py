import pytest

from patchloop.context import ContextBudgetError
from patchloop.domain import PromptCacheLayout, TaskBudget, ToolCall
from patchloop.prompt_cache import (
    AppendOnlyOptimizationPolicy,
    PromptCacheCoordinator,
    PromptPrefixViolation,
    compute_prefix_budget,
)
from patchloop.providers import (
    ChatDialect,
    ModelMessage,
    ModelUsage,
    ProviderAuth,
    ProviderBinding,
    ProviderCapabilities,
    ProviderGeneration,
    ProviderProtocol,
    ToolSpec,
)


def _request_is_prefix(
    previous_messages: list[ModelMessage],
    previous_tools: list[ToolSpec],
    current_messages: list[ModelMessage],
    current_tools: list[ToolSpec],
) -> bool:
    if previous_tools != current_tools or len(previous_messages) > len(current_messages):
        return False
    return [message.model_dump(mode="json") for message in previous_messages] == [
        message.model_dump(mode="json") for message in current_messages[: len(previous_messages)]
    ]


def test_full_request_prefix_comparison_preserves_tool_call_data_and_tool_order() -> None:
    messages = [
        ModelMessage(role="system", content="fixed"),
        ModelMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="call-1", name="inspect", arguments={"path": "a.py"})],
        ),
        ModelMessage(role="tool", content="observed", tool_call_id="call-1"),
    ]
    tools = [
        ToolSpec(name="inspect", description="Read a file.", parameters={"type": "object"}),
        ToolSpec(
            name="write",
            description="Write a file.",
            parameters={"type": "object"},
            permission="write",
        ),
    ]
    appended = [*messages, ModelMessage(role="user", content="continue")]

    assert _request_is_prefix(messages, tools, appended, list(tools))

    changed_call = [message.model_copy(deep=True) for message in appended]
    changed_call[1].tool_calls[0] = ToolCall(
        id="call-2", name="inspect", arguments={"path": "a.py"}
    )
    assert not _request_is_prefix(messages, tools, changed_call, list(tools))
    assert not _request_is_prefix(messages, tools, appended, list(reversed(tools)))


def _tools() -> list[ToolSpec]:
    return [
        ToolSpec(name="inspect", description="Read files.", parameters={"type": "object"}),
        ToolSpec(name="write", description="Write files.", parameters={"type": "object"}),
    ]


def _binding(*, context_window: int = 8_192) -> ProviderBinding:
    return ProviderBinding(
        profile_id="test-provider",
        protocol=ProviderProtocol.CHAT_COMPLETIONS,
        dialect=ChatDialect.STANDARD,
        model="test-model",
        base_url="https://provider.example/v1",
        auth=ProviderAuth.NONE,
        capabilities=ProviderCapabilities(
            tools=True,
            context_window_tokens=context_window,
            max_output_tokens=2_048,
        ),
        generation=ProviderGeneration(max_output_tokens=1_024),
    )


def _submitted_coordinator() -> tuple[
    PromptCacheCoordinator,
    list[ModelMessage],
    list[ToolSpec],
    ProviderBinding,
]:
    root = [
        ModelMessage(role="system", content="fixed system"),
        ModelMessage(role="user", content="private task body"),
    ]
    tools = _tools()
    binding = _binding()
    coordinator = PromptCacheCoordinator.bootstrap(
        root,
        tools,
        layout=PromptCacheLayout.APPEND_ONLY,
        prefix_message_count=2,
    )
    submitted = [*root, ModelMessage(role="assistant", content="private previous response")]
    prepared = coordinator.prepare_request(
        0,
        submitted,
        provider="test-provider",
        model="test-model",
        thinking={"enabled": False},
        provider_binding=binding,
    )
    coordinator.observe_response(prepared, ModelUsage(input_tokens=20, output_tokens=5))
    return coordinator, submitted, tools, binding


def test_append_only_coordinator_accepts_an_unchanged_prefix_with_appended_suffix() -> None:
    coordinator, submitted, _, binding = _submitted_coordinator()
    next_messages = [*submitted, ModelMessage(role="user", content="continue")]
    projection = "ignored by the append-only coordinator"

    assert (
        coordinator.materialize_messages(
            next_messages,
            memory_projection=projection,
        )
        == next_messages
    )
    assert coordinator.publication_snapshot is None

    prepared = coordinator.prepare_request(
        1,
        next_messages,
        provider="test-provider",
        model="test-model",
        thinking={"enabled": False},
        provider_binding=binding,
        memory_projection=projection,
    )

    assert prepared.messages == next_messages
    assert coordinator.publication_messages == []
    assert coordinator.append_only_state is not None
    assert coordinator.append_only_state.last_submitted_message_count == len(next_messages)


@pytest.mark.parametrize("change", ["single_character", "insertion"])
def test_append_only_coordinator_rejects_changes_to_previous_messages_without_body(
    change: str,
) -> None:
    coordinator, submitted, tools, binding = _submitted_coordinator()
    candidate = [message.model_copy(deep=True) for message in submitted]
    if change == "single_character":
        candidate[2].content = "private changed response"
    else:
        candidate.insert(2, ModelMessage(role="user", content="private inserted body"))
    candidate.append(ModelMessage(role="user", content="continue"))

    with pytest.raises(PromptPrefixViolation, match="message") as error:
        coordinator.prepare_request(
            1,
            candidate,
            provider="test-provider",
            model="test-model",
            thinking={"enabled": False},
            provider_binding=binding,
            tools=tools,
        )

    assert "private" not in str(error.value)
    assert error.value.expected_fingerprint is not None
    assert error.value.actual_fingerprint is not None


def test_append_only_coordinator_rejects_tool_order_and_provider_binding_changes() -> None:
    coordinator, submitted, tools, binding = _submitted_coordinator()
    suffix = [*submitted, ModelMessage(role="user", content="continue")]

    with pytest.raises(PromptPrefixViolation, match="tool definitions"):
        coordinator.prepare_request(
            1,
            suffix,
            provider="test-provider",
            model="test-model",
            thinking={"enabled": False},
            provider_binding=binding,
            tools=list(reversed(tools)),
        )

    with pytest.raises(PromptPrefixViolation, match="provider binding"):
        coordinator.prepare_request(
            1,
            suffix,
            provider="test-provider",
            model="test-model",
            thinking={"enabled": False},
            provider_binding=_binding(context_window=16_384),
        )


def test_compute_prefix_budget_uses_declared_window_and_never_returns_negative_limits() -> None:
    task_budget = TaskBudget(max_context_tokens=8_000, max_output_tokens=1_500)
    tools = _tools()

    budget = compute_prefix_budget(task_budget, _binding(context_window=8_192), tools)

    assert budget.input_limit == 8_192 - 1_024 - 256
    assert budget.ordinary_limit < budget.input_limit
    assert budget.soft_limit == int(budget.ordinary_limit * 0.8)
    assert budget.memory_message_limit >= 64
    assert budget.summary_limit >= 128
    offline = compute_prefix_budget(
        TaskBudget(max_context_tokens=16_000, max_output_tokens=4_000), None, []
    )

    balanced = compute_prefix_budget(
        task_budget,
        _binding(context_window=8_192),
        tools,
        policy=AppendOnlyOptimizationPolicy.for_version("balanced_v1"),
    )
    assert balanced.soft_limit == int(balanced.ordinary_limit * 0.95)
    assert offline.input_limit == 16_000
    assert all(
        value >= 0
        for value in (
            offline.input_limit,
            offline.ordinary_limit,
            offline.soft_limit,
            offline.memory_message_limit,
            offline.summary_limit,
        )
    )


@pytest.mark.parametrize("context_tokens,output_tokens", [(256, 100_000), (768, 100_000)])
def test_compute_prefix_budget_rejects_small_windows_as_configuration_errors(
    context_tokens: int,
    output_tokens: int,
) -> None:
    with pytest.raises(ContextBudgetError):
        compute_prefix_budget(
            TaskBudget(max_context_tokens=context_tokens, max_output_tokens=output_tokens),
            None,
            [],
        )
