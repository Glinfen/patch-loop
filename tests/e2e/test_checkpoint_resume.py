import shutil
from pathlib import Path

import pytest

from patchloop.domain import Task, TaskStatus, ToolCall
from patchloop.events import EventLogger
from patchloop.persistence import SQLiteStore
from patchloop.providers import FakeProvider, ModelMessage, ModelResponse, ToolSpec
from patchloop.runtime import AgentRuntime
from patchloop.tools import (
    ApplyPatchTool,
    PermissionLevel,
    RunTestsTool,
    ToolContext,
    ToolGateway,
    ToolPolicy,
    UpdatePlanTool,
)


class InterruptingProvider:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = iter(responses)

    @property
    def name(self) -> str:
        return "interrupting"

    def complete(
        self,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        del messages, tools
        try:
            return next(self.responses)
        except StopIteration:
            raise KeyboardInterrupt("simulated process termination") from None


def test_resume_after_interruption_does_not_repeat_confirmed_write(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "benchmarks" / "fixtures" / "calculator_bug"
    repository = tmp_path / "calculator_bug"
    shutil.copytree(source, repository)
    store = SQLiteStore(tmp_path / "state" / "patchloop.db")
    trace = EventLogger(tmp_path / "trace.jsonl")
    policy = ToolPolicy(
        frozenset({PermissionLevel.READ, PermissionLevel.WRITE, PermissionLevel.EXECUTE})
    )
    first_gateway = ToolGateway(
        ToolContext(repository),
        [UpdatePlanTool(), ApplyPatchTool(), RunTestsTool()],
        trace,
        policy,
    )
    first_provider = InterruptingProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="plan-call",
                        name="update_plan",
                        arguments={
                            "items": [
                                {"description": "Fix divide", "status": "running"},
                                {"description": "Test", "status": "pending"},
                            ]
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="write-call",
                        name="apply_patch",
                        arguments={
                            "path": "calculator.py",
                            "edits": [
                                {
                                    "old_text": "return dividend // divisor",
                                    "new_text": "return dividend / divisor",
                                }
                            ],
                        },
                    )
                ]
            ),
        ]
    )
    task = Task(goal="Resume calculator repair", repository=str(repository))

    with pytest.raises(KeyboardInterrupt, match="simulated process termination"):
        AgentRuntime(first_provider, first_gateway, trace, store).run(task)

    persisted = store.get_task(task.id)
    checkpoint = store.get_checkpoint(task.id)
    assert checkpoint.memory_manager is not None
    before_memory_ids = {record.id for record in store.memory.list_records(task.id)}
    assert checkpoint.memory_manager.cursor.pending_event_ids == []
    assert checkpoint.memory_manager.cursor.processed_event_ids.count("tool:write-call") == 1
    assert checkpoint.memory_manager.read_duration_ms > 0
    assert checkpoint.memory_manager.write_duration_ms > 0
    assert checkpoint.max_memory_context_tokens_used > 0
    assert checkpoint.max_memory_context_occupancy > 0
    assert persisted.status is TaskStatus.RUNNING
    assert checkpoint.next_step_index == 2
    assert "return dividend / divisor" in (repository / "calculator.py").read_text(encoding="utf-8")

    second_gateway = ToolGateway(
        ToolContext(repository),
        [UpdatePlanTool(), ApplyPatchTool(), RunTestsTool()],
        trace,
        policy,
    )
    second_provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="test-call",
                        name="run_tests",
                        arguments={
                            "command": [
                                "python",
                                "-m",
                                "pytest",
                                "-q",
                                "test_calculator.py",
                            ]
                        },
                    )
                ]
            ),
            ModelResponse(content="Resumed and verified without repeating the write."),
        ]
    )

    result = AgentRuntime(second_provider, second_gateway, trace, store).resume(
        persisted, checkpoint
    )

    assert result.status is TaskStatus.COMPLETED
    assert result.report is not None
    assert result.report.changed_files == ["calculator.py"]
    assert result.report.validations[0].passed
    assert result.report.max_memory_context_tokens_used >= checkpoint.max_memory_context_tokens_used
    assert result.report.memory_read_duration_ms >= checkpoint.memory_manager.read_duration_ms
    assert store.get_task(task.id).status is TaskStatus.COMPLETED
    assert len(store.list_steps(task.id)) == 4
    assert len(store.list_tool_results(task.id)) == 3
    write_events = [
        event
        for event in trace.read()
        if event.type == "tool.completed" and event.data["call"]["name"] == "apply_patch"
    ]
    assert len(write_events) == 1
    final_checkpoint = store.get_checkpoint(task.id)
    assert final_checkpoint.memory_manager is not None
    assert before_memory_ids.issubset({record.id for record in store.memory.list_records(task.id)})
    assert final_checkpoint.memory_manager.cursor.processed_event_ids.count("tool:write-call") == 1
