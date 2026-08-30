from __future__ import annotations

from pathlib import Path

from patchloop.domain import Task, TaskBudget, TaskStatus, ToolCall
from patchloop.events import EventLogger
from patchloop.memory import MemoryKind, MemoryQuery, WorkingMemoryItemKind
from patchloop.observability import TaskMetrics
from patchloop.persistence import SQLiteStore
from patchloop.providers import FakeProvider, ModelMessage, ModelResponse, ToolSpec
from patchloop.runtime import AgentRuntime
from patchloop.tools import ReadFileTool, ToolContext, ToolGateway, UpdatePlanTool


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
    checkpoint = store.get_checkpoint(task.id)
    assert checkpoint.next_step_index == 100
    assert checkpoint.working_memory is not None
    assert checkpoint.working_memory.estimated_tokens <= 600
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
