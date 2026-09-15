from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import Field

from patchloop.domain import (
    PromptCacheLayout,
    Task,
    TaskBudget,
    TaskExecutionConfig,
    TaskStatus,
    ToolCall,
)
from patchloop.events import EventLogger
from patchloop.memory.retrieval import (
    RetrievalLayer,
    RetrievalSelection,
)
from patchloop.persistence import SQLiteStore
from patchloop.prompt_cache import MemoryDeltaPublisher
from patchloop.providers import FakeProvider, ModelMessage, ModelResponse
from patchloop.runtime import AgentRuntime
from patchloop.session.service import SessionService
from patchloop.tools.base import Tool, ToolContext, ToolInputModel
from patchloop.tools.gateway import ToolGateway


class _ObservationInput(ToolInputModel):
    index: int = Field(ge=0, le=5)


class _ObservationTool(Tool):
    name = "prefix_fixture_observe"
    description = "Return a deterministic observation for prompt-prefix regression tests."
    input_model = _ObservationInput

    def run(self, arguments, context: ToolContext) -> str:
        del context
        index = _ObservationInput.model_validate(arguments).index
        if index == 2:
            return f"observation-{index}: " + ("long tool output;" * 1_200)
        return f"observation-{index}: stable tool output"


def _tool_responses(count: int = 6) -> list[ModelResponse]:
    responses = [
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id=f"observation-{index}",
                    name="prefix_fixture_observe",
                    arguments={"index": index},
                )
            ]
        )
        for index in range(count)
    ]
    responses.append(ModelResponse(content="Completed the observations."))
    return responses


def _run_runtime(
    tmp_path: Path,
    layout: PromptCacheLayout,
    *,
    responses: list[ModelResponse] | None = None,
    budget: TaskBudget | None = None,
    runtime_type: type[AgentRuntime] = AgentRuntime,
    runtime_kwargs: dict[str, object] | None = None,
) -> tuple[FakeProvider, EventLogger, Task, AgentRuntime]:
    repository = tmp_path / "repository"
    repository.mkdir(parents=True, exist_ok=True)
    trace = EventLogger(tmp_path / "trace.jsonl")
    provider = FakeProvider(responses or _tool_responses())
    gateway = ToolGateway(ToolContext(repository), [_ObservationTool()], trace)
    runtime = runtime_type(provider, gateway, trace, **(runtime_kwargs or {}))
    task = Task(
        goal="Inspect the repository observations and preserve the task constraints.",
        repository=str(repository),
        budget=budget or TaskBudget(max_steps=10),
        execution=TaskExecutionConfig(prompt_cache_layout=layout),
    )
    result = runtime.run(task)
    assert result.status is TaskStatus.COMPLETED, result.error
    return provider, trace, result, runtime


def _messages_are_prefix(previous: list[ModelMessage], current: list[ModelMessage]) -> bool:
    if len(previous) > len(current):
        return False
    return [message.model_dump(mode="json") for message in previous] == [
        message.model_dump(mode="json") for message in current[: len(previous)]
    ]


def test_real_runtime_reproduces_legacy_system_rewrite_and_stable_memory_insertion(
    tmp_path: Path,
) -> None:
    legacy_provider, _, _, _ = _run_runtime(tmp_path / "legacy", PromptCacheLayout.LEGACY)
    stable_provider, _, _, _ = _run_runtime(tmp_path / "stable", PromptCacheLayout.STABLE)

    assert len(legacy_provider.requests) == 7
    assert len(stable_provider.requests) == 7
    legacy_first = legacy_provider.requests[0][0]
    legacy_second = legacy_provider.requests[1][0]
    assert legacy_first[0].role == legacy_second[0].role == "system"
    assert legacy_first[0].content != legacy_second[0].content
    assert not _messages_are_prefix(legacy_first, legacy_second)

    stable_second = stable_provider.requests[1][0]
    stable_third = stable_provider.requests[2][0]
    assert _messages_are_prefix(stable_provider.requests[0][0], stable_second)
    assert not _messages_are_prefix(stable_second, stable_third)
    assert all(stable_provider.requests[0][1] == tools for _, tools in stable_provider.requests)
    tool_outputs = [
        json.loads(message.content)
        for messages, _ in stable_provider.requests
        for message in messages
        if message.role == "tool"
    ]
    long_output = next(
        output
        for output in tool_outputs
        if output["tool_name"] == "prefix_fixture_observe"
        and output["output"].startswith("observation-2")
    )
    assert long_output["output_truncated"] is True
    assert long_output["original_output_chars"] > TaskBudget().max_tool_output_chars
    assert len(long_output["output"]) <= TaskBudget().max_tool_output_chars


class _ProjectionSequenceRuntime(AgentRuntime):
    def __init__(self, *args, projections: list[str], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._projections = iter(projections)

    def _retrieve_memory(self, task, total_context_tokens, retrieval_token_cap=None):
        retrieved = super()._retrieve_memory(task, total_context_tokens, retrieval_token_cap)
        marker = next(self._projections)
        if retrieved.context is None:
            raise AssertionError("the real MemoryManager should return a retrieval context")
        selection = RetrievalSelection(
            id=f"prefix-stimulus-{marker}",
            layer=RetrievalLayer.WORKING,
            text=marker,
            score=1.0,
            estimated_tokens=1,
            reason="deterministic cache regression stimulus",
            diversity_key="prefix-stimulus",
            provider_text=marker,
        )
        context = retrieved.context.model_copy(update={"selections": [selection]})
        return replace(retrieved, context=context)


def test_same_memory_retrieval_is_deduplicated_in_the_runtime_request_stream(
    tmp_path: Path,
) -> None:
    provider, _, _, runtime = _run_runtime(
        tmp_path,
        PromptCacheLayout.STABLE,
        runtime_type=_ProjectionSequenceRuntime,
        runtime_kwargs={"projections": ["same"] * 7},
    )

    assert len(provider.requests) == 7
    snapshot = runtime._prompt_cache.publication_snapshot
    assert snapshot is not None
    assert snapshot.delta_count == 0
    assert MemoryDeltaPublisher.replay(snapshot)["working_state"][0]["text"] == "same"


def test_real_runtime_exposes_v1_publisher_a_to_b_to_a_to_b_repeat_bug(
    tmp_path: Path,
) -> None:
    provider, _, _, runtime = _run_runtime(
        tmp_path,
        PromptCacheLayout.STABLE,
        responses=_tool_responses(count=4),
        runtime_type=_ProjectionSequenceRuntime,
        runtime_kwargs={"projections": ["A", "B", "A", "B", "B"]},
    )

    assert len(provider.requests) == 5
    snapshot = runtime._prompt_cache.publication_snapshot
    assert snapshot is not None
    # The repeated A→B delta is globally deduplicated by V1, leaving the replayed state at A.
    assert MemoryDeltaPublisher.replay(snapshot)["working_state"][0]["text"] == "A"


def test_runtime_compression_request_is_recorded_with_its_source_history(tmp_path: Path) -> None:
    responses = _tool_responses(count=3)
    responses.insert(2, ModelResponse(content='{"summary":"keep the observed constraints"}'))
    provider, trace, _, _ = _run_runtime(
        tmp_path,
        PromptCacheLayout.STABLE,
        responses=responses,
        budget=TaskBudget(max_steps=8, max_context_tokens=1_024, max_tool_output_chars=8_000),
    )

    compression = [
        messages
        for messages, _ in provider.requests
        if any("PATCHLOOP_EPOCH_COMPRESSION_V1" in message.content for message in messages)
    ]
    assert compression
    assert any(event.type == "cache.compression.requested" for event in trace.read())
    assert any(message.role == "tool" for message in compression[0])


@pytest.mark.parametrize("layout", [PromptCacheLayout.STABLE, PromptCacheLayout.APPEND_ONLY])
def test_runtime_appends_a_pending_session_turn_after_the_completed_tool_group(
    tmp_path: Path,
    layout: PromptCacheLayout,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "session.db")
    service = SessionService(store)
    session = service.create(str(repository), session_id="prefix-session")
    task = service.start_task(
        session.id,
        "Inspect a repository and honor new user input.",
        task_id="prefix-pending-input",
        execution=TaskExecutionConfig(prompt_cache_layout=layout),
    )

    class InputSubmittingProvider(FakeProvider):
        submitted = False

        def complete(self, messages, tools):
            response = super().complete(messages, tools)
            if not self.submitted:
                self.submitted = True
                service.append_message(
                    session.id,
                    "Also preserve the newly submitted constraint.",
                    client_submission_id="late-constraint",
                )
            return response

    provider = InputSubmittingProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="pending-input-tool",
                        name="prefix_fixture_observe",
                        arguments={"index": 0},
                    )
                ]
            ),
            ModelResponse(content="Completed with the new constraint."),
        ]
    )
    trace = EventLogger(tmp_path / "pending-input.jsonl")
    runtime = AgentRuntime(
        provider,
        ToolGateway(ToolContext(repository), [_ObservationTool()], trace),
        trace,
        store,
    )

    result = runtime.run(task)

    assert result.status is TaskStatus.COMPLETED, result.error
    assert len(provider.requests) == 2
    second_request = provider.requests[1][0]
    assert any(
        message.role == "user"
        and message.content == "Also preserve the newly submitted constraint."
        for message in second_request
    )
    tool_call = next(message for message in second_request if message.tool_calls)
    tool_result = next(message for message in second_request if message.role == "tool")
    assert tool_result.tool_call_id == tool_call.tool_calls[0].id


def test_append_only_runtime_preserves_six_rounds_and_cyclic_memory(tmp_path: Path) -> None:
    provider, _, _, runtime = _run_runtime(
        tmp_path,
        PromptCacheLayout.APPEND_ONLY,
        runtime_type=_ProjectionSequenceRuntime,
        runtime_kwargs={"projections": ["A", "B", "A", "B", "C", "C", "C"]},
    )
    assert len(provider.requests) == 7
    for (previous, tools), (current, current_tools) in zip(
        provider.requests,
        provider.requests[1:],
        strict=False,
    ):
        assert _messages_are_prefix(previous, current)
        assert tools == current_tools
    snapshot = runtime._prompt_cache.publication_snapshot
    assert snapshot.schema_version == "2.0"
    assert snapshot.delta_count == 4
    assert MemoryDeltaPublisher.replay(snapshot)["working_state"][0]["text"] == "C"
