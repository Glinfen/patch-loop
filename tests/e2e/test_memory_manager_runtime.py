from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from patchloop.domain import Task, TaskStatus, ToolCall
from patchloop.events import EventLogger
from patchloop.memory import CompressionReport, MemoryRecord, MemorySource, MemoryStoreError
from patchloop.persistence import SQLiteStore
from patchloop.providers import ModelMessage, ModelResponse, ToolSpec
from patchloop.runtime import AgentRuntime
from patchloop.tools import ReadFileTool, ToolContext, ToolGateway


class WriteFailingMemoryStore:
    def list_records(self, task_id: str) -> list[MemoryRecord]:
        del task_id
        return []

    def list_sources(self, task_id: str) -> list[MemorySource]:
        del task_id
        return []

    def save_batch(
        self,
        *,
        sources: Sequence[MemorySource] = (),
        records: Sequence[MemoryRecord] = (),
        compactions: Sequence[CompressionReport] = (),
    ) -> object:
        del sources, records, compactions
        raise MemoryStoreError("simulated memory database failure")


class FallbackProvider:
    def __init__(self) -> None:
        self.index = 0
        self.final_system = ""

    @property
    def name(self) -> str:
        return "fallback-probe"

    def complete(
        self,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        del tools
        if self.index == 0:
            response = ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="fallback-read",
                        name="read_file",
                        arguments={"path": "config.py"},
                    )
                ]
            )
        else:
            self.final_system = messages[0].content
            response = ModelResponse(content="Completed through the legacy context fallback.")
        self.index += 1
        return response


def test_memory_write_failure_falls_back_without_failing_task_or_tool(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "config.py").write_text("MODE = 'safe'\n", encoding="utf-8")
    runtime_store = SQLiteStore(tmp_path / "state.db")
    runtime_store.memory = WriteFailingMemoryStore()  # type: ignore[assignment]
    trace = EventLogger(tmp_path / "trace.jsonl")
    provider = FallbackProvider()
    gateway = ToolGateway(ToolContext(repository), [ReadFileTool()], trace)
    task = Task(id="runtime-fallback", goal="Inspect config.py", repository=str(repository))

    result = AgentRuntime(provider, gateway, trace, runtime_store).run(task)

    assert result.status is TaskStatus.COMPLETED, result.error
    assert result.report is not None
    assert result.report.memory_fallbacks == 1
    assert result.report.memory_events_ingested == 3
    assert "PATCHLOOP_LAYERED_MEMORY_V1" not in provider.final_system
    assert len(runtime_store.list_tool_results(task.id)) == 1
    events = trace.read()
    fallback = [event for event in events if event.type == "memory.fallback"]
    assert len(fallback) == 1
    assert fallback[0].data["strategy"] == "task_memory_v1"
    checkpoint = runtime_store.get_checkpoint(task.id)
    assert checkpoint.memory_manager is not None
    assert checkpoint.memory_manager.fallback_active
    assert checkpoint.memory_manager.cursor.pending_event_ids == []
