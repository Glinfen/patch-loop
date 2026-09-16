from __future__ import annotations

from pathlib import Path

from patchloop.domain import (
    PromptCacheLayout,
    Task,
    TaskBudget,
    TaskExecutionConfig,
    TaskRuntimeCondition,
    TaskStatus,
    ToolCall,
)
from patchloop.events import EventLogger
from patchloop.memory import (
    MemoryKind,
    MemoryProjectionBudgetError,
    MemoryQuery,
    WorkingMemoryItemKind,
)
from patchloop.observability import TaskMetrics
from patchloop.persistence import SQLiteStore
from patchloop.prompt_cache import MemoryDeltaPublisher
from patchloop.providers import FakeProvider, ModelMessage, ModelResponse, ToolSpec
from patchloop.runtime import AgentRuntime
from patchloop.tools import ReadFileTool, ToolContext, ToolGateway, UpdatePlanTool


def test_balanced_append_only_runtime_publishes_structured_working_memory(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    trace = EventLogger(tmp_path / "trace.jsonl")
    provider = FakeProvider([ModelResponse(content="Constraint preserved.")])
    runtime = AgentRuntime(provider, ToolGateway(ToolContext(repository), [], trace), trace, store)
    task = Task(
        id="balanced-structured-working",
        goal="Only inspect src. Do not modify .env.",
        repository=str(repository),
        execution=TaskExecutionConfig(
            prompt_cache_layout=PromptCacheLayout.APPEND_ONLY,
            append_only_optimization="balanced_v1",
        ),
    )

    result = runtime.run(task)

    assert result.status is TaskStatus.COMPLETED, result.error
    snapshot = runtime._prompt_cache.publication_snapshot
    assert snapshot is not None
    working = MemoryDeltaPublisher.replay(snapshot)["working_state"]
    assert working
    assert all(item.get("type") == "working_memory" for item in working)
    assert all("key" in item and "value" in item for item in working)
    assert all(set(item) != {"text"} for item in working)


def test_balanced_projection_budget_error_pauses_before_provider_request(tmp_path: Path) -> None:
    class ProjectionOverflowRuntime(AgentRuntime):
        def _retrieve_memory(self, *args, **kwargs):
            raise MemoryProjectionBudgetError(
                required_tokens=400,
                available_tokens=128,
                required_keys=["constraint:required"],
            )

    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    trace = EventLogger(tmp_path / "trace.jsonl")
    provider = FakeProvider([])
    task = Task(
        id="balanced-projection-overflow",
        goal="Only preserve the required constraint.",
        repository=str(repository),
        execution=TaskExecutionConfig(
            prompt_cache_layout=PromptCacheLayout.APPEND_ONLY,
            append_only_optimization="balanced_v1",
        ),
    )

    result = ProjectionOverflowRuntime(
        provider,
        ToolGateway(ToolContext(repository), [], trace),
        trace,
        store,
    ).run(task)

    assert result.runtime_condition is TaskRuntimeCondition.PAUSED
    assert result.status is TaskStatus.RUNNING
    assert provider.requests == []
    event = next(item for item in trace.read() if item.type == "context.budget_exceeded")
    assert event.data["required_keys"] == ["constraint:required"]


class LongContextProvider:
    def __init__(self) -> None:
        self.request_index = 0
        self.saw_constraint_every_turn = True
        self.saw_active_error_after_failure = False

    @property
    def name(self) -> str:
        return "long-context"

    def complete(
        self,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        del tools
        joined = "\n".join(message.content for message in messages)
        assert "PATCHLOOP_WORKING_MEMORY_V1" in joined
        self.saw_constraint_every_turn &= "Only inspect src" in joined
        self.saw_constraint_every_turn &= "Do not modify .env" in joined
        if self.request_index > 50 and "missing_tool [unknown_tool]" in joined:
            self.saw_active_error_after_failure = True
        if self.request_index == 100:
            return ModelResponse(content="Completed the 100-step inspection.")
        if self.request_index == 50:
            call = ToolCall(id="missing-50", name="missing_tool")
        else:
            line = self.request_index + 1
            call = ToolCall(
                id=f"read-{self.request_index}",
                name="read_file",
                arguments={
                    "path": "src/data.txt",
                    "start_line": line,
                    "end_line": line,
                },
            )
        self.request_index += 1
        return ModelResponse(tool_calls=[call])


def test_runtime_keeps_working_state_bounded_for_100_steps(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    source = repository / "src"
    source.mkdir(parents=True)
    (source / "data.txt").write_text(
        "\n".join(f"result-{index} " + "payload " * 20 for index in range(1, 101)),
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / "state.db")
    trace = EventLogger(tmp_path / "trace.jsonl")
    provider = LongContextProvider()
    gateway = ToolGateway(ToolContext(repository), [ReadFileTool()], trace)
    task = Task(
        id="working-memory-100",
        goal="Only inspect src. Do not modify .env.",
        repository=str(repository),
        budget=TaskBudget(
            max_steps=102,
            max_context_tokens=4_000,
            max_working_memory_tokens=600,
            max_repeated_actions=10,
            max_tool_failures=2,
            context_recent_steps=1,
        ),
    )

    result = AgentRuntime(provider, gateway, trace, store).run(task)

    assert result.status is TaskStatus.COMPLETED, result.error
    assert provider.saw_constraint_every_turn
    assert provider.saw_active_error_after_failure
    assert result.report is not None
    assert result.report.tool_calls == 100
    assert result.report.working_memory_evictions > 0
    assert result.report.max_working_memory_tokens_used <= 600
    assert result.report.memory_compactions > 0
    assert result.report.memory_compression_output_tokens < (
        result.report.memory_compression_input_tokens
    )
    checkpoint = store.get_checkpoint(task.id)
    assert checkpoint.next_step_index == 100
    assert checkpoint.working_memory is not None
    assert checkpoint.working_memory.estimated_tokens <= 600
    assert checkpoint.memory_manager is not None
    assert checkpoint.memory_manager.cursor.next_event_index == 201
    assert checkpoint.memory_manager.cursor.pending_event_ids == []
    assert checkpoint.memory_manager.compactions == result.report.memory_compactions
    assert any(
        item.kind is WorkingMemoryItemKind.ACTIVE_ERROR for item in checkpoint.working_memory.items
    )
    updates = [event for event in trace.read() if event.type == "working_memory.updated"]
    assert len(updates) == 100
    assert max(int(event.data["estimated_tokens"]) for event in updates) <= 600
    metrics = TaskMetrics.from_events(task.id, trace.read())
    assert metrics.working_memory_updates == 100
    assert metrics.working_memory_evictions == result.report.working_memory_evictions
    assert metrics.max_working_memory_tokens_used <= 600
    compacted = [event for event in trace.read() if event.type == "memory.compacted"]
    assert len(compacted) == result.report.memory_compactions


def test_runtime_persists_completed_phase_promotions(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    trace = EventLogger(tmp_path / "trace.jsonl")
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="plan-running",
                        name="update_plan",
                        arguments={
                            "items": [{"description": "Implement parser", "status": "running"}]
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="plan-completed",
                        name="update_plan",
                        arguments={
                            "items": [
                                {
                                    "description": "Implement parser",
                                    "status": "completed",
                                    "evidence": ["parser tests passed"],
                                }
                            ]
                        },
                    )
                ]
            ),
            ModelResponse(content="Parser phase completed."),
        ]
    )
    gateway = ToolGateway(ToolContext(repository), [UpdatePlanTool()], trace)
    task = Task(id="working-promotion", goal="Implement parser", repository=str(repository))

    result = AgentRuntime(provider, gateway, trace, store).run(task)

    assert result.status is TaskStatus.COMPLETED, result.error
    bundle = store.memory.query(
        MemoryQuery(
            task_id=task.id,
            text="completed parser phase",
            kinds=[MemoryKind.SEMANTIC, MemoryKind.EPISODIC],
            max_results=10,
        )
    )
    assert {hit.record.kind for hit in bundle.hits} == {
        MemoryKind.SEMANTIC,
        MemoryKind.EPISODIC,
    }
    assert all(hit.record.source_ids for hit in bundle.hits)
    assert result.report is not None
    assert result.report.memory_promotions == 2
    promotion_events = [event for event in trace.read() if event.type == "memory.promoted"]
    assert len(promotion_events) == 1
    assert promotion_events[0].data["records"] == 2
