from __future__ import annotations

import json
from pathlib import Path

from patchloop.domain import Task, TaskStatus, ToolCall
from patchloop.events import EventLogger
from patchloop.observability import TaskMetrics, TaskReplay
from patchloop.persistence import SQLiteStore
from patchloop.providers import ModelMessage, ModelResponse, ToolSpec
from patchloop.runtime import AgentRuntime
from patchloop.tools import (
    ApplyPatchTool,
    PermissionLevel,
    ReadFileTool,
    ToolContext,
    ToolGateway,
    ToolPolicy,
    UpdatePlanTool,
)


class LayeredMemoryProvider:
    def __init__(self) -> None:
        self.index = 0
        self.saw_current_fact = False
        self.saw_selection_reason = False
        self.last_system = ""

    @property
    def name(self) -> str:
        return "layered-memory"

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
                        id="layered-plan",
                        name="update_plan",
                        arguments={"items": [{"description": "Set MODE new", "status": "running"}]},
                    )
                ]
            )
        elif self.index == 1:
            response = ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="layered-read",
                        name="read_file",
                        arguments={"path": "config.py"},
                    )
                ]
            )
        elif self.index == 2:
            response = ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="layered-patch",
                        name="apply_patch",
                        arguments={
                            "path": "config.py",
                            "edits": [{"old_text": 'MODE = "old"', "new_text": 'MODE = "new"'}],
                        },
                    )
                ]
            )
        else:
            system = messages[0].content
            self.last_system = system
            retrieved = system.split("PATCHLOOP_RETRIEVED_LONG_TERM_V1", 1)[-1]
            payload = json.loads(retrieved)
            retrieved_text = "\n".join(str(item["text"]) for item in payload)
            self.saw_current_fact = (
                "PATCHLOOP_RETRIEVED_LONG_TERM_V1" in system
                and 'value="new"' in retrieved_text
                and 'value="old"' not in retrieved_text
            )
            self.saw_selection_reason = "selection_reasons" in system and "hybrid:" in system
            response = ModelResponse(content="Used only the active MODE fact.")
        self.index += 1
        return response


def test_runtime_retrieves_only_active_cross_layer_facts_with_reasons(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "config.py").write_text('MODE = "old"\n', encoding="utf-8")
    trace = EventLogger(tmp_path / "trace.jsonl")
    store = SQLiteStore(tmp_path / "state.db")
    provider = LayeredMemoryProvider()
    gateway = ToolGateway(
        ToolContext(repository),
        [UpdatePlanTool(), ReadFileTool(), ApplyPatchTool()],
        trace,
        ToolPolicy(frozenset({PermissionLevel.READ, PermissionLevel.WRITE})),
    )
    task = Task(
        id="layered-runtime",
        goal="Set config.py MODE to new and use only the current value",
        repository=str(repository),
    )

    result = AgentRuntime(provider, gateway, trace, store).run(task)

    assert result.status is TaskStatus.COMPLETED, result.error
    assert provider.saw_current_fact, provider.last_system
    assert provider.saw_selection_reason
    events = [event for event in trace.read() if event.type == "memory.retrieved"]
    assert len(events) == 4
    final_selected = events[-1].data["selected"]
    assert isinstance(final_selected, list)
    record_selections = [
        item for item in final_selected if isinstance(item, dict) and item["record_id"] is not None
    ]
    assert record_selections
    assert all(item["reason"] for item in record_selections)
    assert all(item["status"] == "active" for item in record_selections)
    assert all(item["source_ids"] for item in record_selections)
    assert all(item["score_components"] for item in record_selections)
    assert result.report is not None
    assert result.report.memory_retrievals == 4
    assert result.report.memory_retrieval_hits >= 1
    assert result.report.memory_retrieval_tokens > 0
    assert result.report.memory_read_duration_ms > 0
    assert result.report.memory_write_duration_ms > 0
    assert result.report.max_memory_context_tokens_used > 0
    assert result.report.max_memory_context_occupancy > 0
    assert result.report.memory_stale_hits == 0
    assert result.report.memory_records_by_kind["semantic"] > 0
    metrics = TaskMetrics.from_events(task.id, trace.read())
    assert metrics.memory_retrievals == result.report.memory_retrievals
    assert metrics.memory_retrieval_hits == result.report.memory_retrieval_hits
    assert metrics.memory_retrieval_tokens == result.report.memory_retrieval_tokens
    assert metrics.memory_read_duration_ms > 0
    assert metrics.memory_write_duration_ms > 0
    assert metrics.memory_stale_hits == 0
    assert metrics.memory_records_by_kind == result.report.memory_records_by_kind
    replay = TaskReplay.from_events(task.id, trace.read())
    assert len(replay.memory_decisions) == result.report.memory_retrievals
    assert replay.memory_decisions[-1].selected_record_ids
    assert replay.memory_decisions[-1].selections[0]["reason"]
    assert any(event.type == "memory.superseded" for event in trace.read())
    checkpoint = store.get_checkpoint(task.id)
    assert checkpoint.memory_retrievals == 3
