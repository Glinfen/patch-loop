import json

import pytest

from patchloop.context import ContextBudgetError, ContextEngine
from patchloop.domain import Plan, PlanItem, StepStatus, ToolCall, ToolResult
from patchloop.providers import ModelMessage


def tool_group(step: int, output: str) -> list[ModelMessage]:
    call = ToolCall(id=f"call-{step}", name="read_file", arguments={"path": f"file{step}.py"})
    result = ToolResult(
        call_id=call.id,
        tool_name=call.name,
        success=True,
        output=output,
    )
    engine = ContextEngine(max_tokens=700, max_tool_output_chars=160, recent_steps=1)
    observation, _ = engine.compact_tool_result(result)
    return [
        ModelMessage(role="assistant", content=f"inspect step {step}", tool_calls=[call]),
        ModelMessage(role="tool", content=observation, tool_call_id=call.id),
    ]


def test_context_engine_compacts_history_into_structured_memory() -> None:
    engine = ContextEngine(max_tokens=700, max_tool_output_chars=160, recent_steps=3)
    messages = [
        ModelMessage(role="system", content="system"),
        ModelMessage(role="user", content="Use EARLY-CONCLUSION to finish verification"),
        *tool_group(0, "EARLY-CONCLUSION=blue-widget\n" + "x" * 2_000),
        *tool_group(1, "noise one " + "a" * 2_000),
        *tool_group(2, "noise two " + "b" * 2_000),
        *tool_group(3, "noise three " + "c" * 2_000),
    ]
    plan = Plan(
        items=[
            PlanItem(description="Locate the conclusion", status=StepStatus.COMPLETED),
            PlanItem(description="Verify blue widget", status=StepStatus.RUNNING),
        ]
    )

    window = engine.build(messages, [], plan)

    assert window.debug.estimated_tokens <= window.debug.budget_tokens
    assert window.debug.dropped_steps
    assert window.memory is not None
    assert any("Verify blue widget" in item for item in window.memory.unfinished_items)
    assert any(
        "EARLY-CONCLUSION=blue-widget" in evidence.summary
        for evidence in window.memory.key_evidence
    )
    assert "PATCHLOOP_TASK_MEMORY_V1" in "\n".join(message.content for message in window.messages)
    assert "context [" in window.debug.render()
    for index, message in enumerate(window.messages):
        if message.role == "assistant" and message.tool_calls:
            expected_ids = {call.id for call in message.tool_calls}
            actual_ids = {
                item.tool_call_id for item in window.messages[index + 1 :] if item.role == "tool"
            }
            assert expected_ids <= actual_ids


def test_tool_observation_keeps_full_result_outside_context() -> None:
    engine = ContextEngine(max_tokens=500, max_tool_output_chars=128, recent_steps=1)
    result = ToolResult(
        call_id="large-call",
        tool_name="read_file",
        success=True,
        output="HEAD-EVIDENCE\n" + "x" * 2_000 + "\nTAIL-EVIDENCE",
    )

    observation, truncated = engine.compact_tool_result(result)
    payload = json.loads(observation)

    assert truncated
    assert payload["output_truncated"] is True
    assert payload["original_output_chars"] == len(result.output)
    assert "HEAD-EVIDENCE" in payload["output"]
    assert "TAIL-EVIDENCE" in payload["output"]
    assert len(result.output) > len(payload["output"])


def test_context_engine_rejects_mandatory_content_over_budget() -> None:
    engine = ContextEngine(max_tokens=256, max_tool_output_chars=128, recent_steps=1)
    messages = [
        ModelMessage(role="system", content="system"),
        ModelMessage(role="user", content="oversized " * 1_000),
    ]

    with pytest.raises(ContextBudgetError, match="mandatory context requires"):
        engine.build(messages, [], None)


def test_context_redacts_credentials_before_provider_messages() -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    engine = ContextEngine(max_tokens=1_000, max_tool_output_chars=256, recent_steps=1)
    messages = [
        ModelMessage(role="system", content="system"),
        ModelMessage(role="user", content=f"api_key={secret}"),
    ]

    window = engine.build(messages, [], None)

    assert secret not in window.messages[1].content
    assert "[REDACTED]" in window.messages[1].content


def test_context_engine_enforces_explicit_recent_history_budget() -> None:
    engine = ContextEngine(max_tokens=1_200, max_tool_output_chars=160, recent_steps=3)
    messages = [
        ModelMessage(role="system", content="system"),
        ModelMessage(role="user", content="retain relevant evidence"),
        *tool_group(0, "relevant evidence " + "a" * 1_000),
        *tool_group(1, "recent evidence " + "b" * 1_000),
        *tool_group(2, "latest evidence " + "c" * 1_000),
    ]

    window = engine.build(messages, [], None, history_token_budget=240)

    assert window.debug.history_budget_tokens == 240
    assert window.debug.history_tokens <= 240
    assert window.debug.estimated_tokens <= 1_200
    assert window.debug.dropped_steps


def test_layered_context_can_disable_legacy_task_memory_without_breaking_groups() -> None:
    engine = ContextEngine(max_tokens=700, max_tool_output_chars=160, recent_steps=1)
    messages = [
        ModelMessage(role="system", content="layered memory already injected"),
        ModelMessage(role="user", content="retain current evidence"),
        *tool_group(0, "stale evidence " + "a" * 1_000),
        *tool_group(1, "current evidence " + "b" * 1_000),
        *tool_group(2, "latest evidence " + "c" * 1_000),
    ]

    window = engine.build(
        messages,
        [],
        None,
        history_token_budget=240,
        enable_task_memory=False,
    )

    assert window.memory is None
    assert window.debug.memory_tokens == 0
    assert window.debug.history_tokens <= 240
    assert window.debug.dropped_steps
    assert all("PATCHLOOP_TASK_MEMORY_V1" not in item.content for item in window.messages)


def test_layered_context_masks_superseded_values_only_from_audit_history() -> None:
    old_value = "RETURN_NONE_ON_MISSING__OLD"
    messages = [
        ModelMessage(role="system", content="system"),
        ModelMessage(role="user", content=f"Replace {old_value} with the new contract"),
        *tool_group(0, f"MODE={old_value}"),
    ]

    window = ContextEngine(
        max_tokens=800,
        max_tool_output_chars=256,
        recent_steps=1,
    ).build(
        messages,
        [],
        None,
        enable_task_memory=False,
        excluded_history_values=[old_value],
    )

    assert old_value in window.messages[1].content
    history = "\n".join(message.content for message in window.messages[2:])
    assert old_value not in history
    assert "[SUPERSEDED_MEMORY_OMITTED]" in history
