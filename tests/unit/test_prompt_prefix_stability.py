from patchloop.domain import ToolCall
from patchloop.providers import ModelMessage, ToolSpec


def _request_is_prefix(
    previous_messages: list[ModelMessage],
    previous_tools: list[ToolSpec],
    current_messages: list[ModelMessage],
    current_tools: list[ToolSpec],
) -> bool:
    if previous_tools != current_tools or len(previous_messages) > len(current_messages):
        return False
    return [message.model_dump(mode="json") for message in previous_messages] == [
        message.model_dump(mode="json")
        for message in current_messages[: len(previous_messages)]
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
