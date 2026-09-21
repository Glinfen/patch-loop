from __future__ import annotations

from pathlib import Path

from patchloop.domain import Task, TaskStatus, ToolCall
from patchloop.events import EventLogger
from patchloop.memory import MemoryKind, MemoryQuery, MemoryStatus, validate_supersession_chain
from patchloop.observability import TaskMetrics
from patchloop.persistence import SQLiteStore
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.runtime import AgentRuntime
from patchloop.sandbox import LocalProcessSandbox
from patchloop.tools import (
    ApplyPatchTool,
    PermissionLevel,
    ReadFileTool,
    RunTestsTool,
    ToolContext,
    ToolGateway,
    ToolPolicy,
    UpdatePlanTool,
)


def test_runtime_replaces_stale_fact_and_promotes_test_verified_value(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "config.py").write_text('MODE = "old"\n', encoding="utf-8")
    (repository / "test_config.py").write_text(
        'from config import MODE\n\n\ndef test_mode():\n    assert MODE == "new"\n',
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path / "state.db")
    trace = EventLogger(tmp_path / "trace.jsonl")
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="semantic-plan",
                        name="update_plan",
                        arguments={
                            "items": [
                                {"description": "Update mode and verify", "status": "running"}
                            ]
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="semantic-read",
                        name="read_file",
                        arguments={"path": "config.py"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="semantic-patch",
                        name="apply_patch",
                        arguments={
                            "path": "config.py",
                            "edits": [{"old_text": 'MODE = "old"', "new_text": 'MODE = "new"'}],
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="semantic-tests",
                        name="run_tests",
                        arguments={"command": ["python", "-m", "pytest", "-q", "test_config.py"]},
                    )
                ]
            ),
            ModelResponse(content="Updated and verified the active mode."),
        ]
    )
    gateway = ToolGateway(
        ToolContext(repository, LocalProcessSandbox()),
        [UpdatePlanTool(), ReadFileTool(), ApplyPatchTool(), RunTestsTool()],
        trace,
        ToolPolicy(
            frozenset({PermissionLevel.READ, PermissionLevel.WRITE, PermissionLevel.EXECUTE})
        ),
    )
    task = Task(
        id="semantic-runtime",
        goal="Only update MODE. Do not edit the test.",
        repository=str(repository),
    )

    result = AgentRuntime(provider, gateway, trace, store).run(task)

    assert result.status is TaskStatus.COMPLETED, result.error
    active = store.memory.query(
        MemoryQuery(
            task_id=task.id,
            text="config MODE value",
            kinds=[MemoryKind.SEMANTIC],
            paths=["config.py"],
            fact_types=["code_symbol"],
        )
    )
    assert len(active.hits) == 1
    current = active.hits[0].record
    assert current.content["value"] == '"new"'
    assert current.content["epistemic_status"] == "verified"
    assert current.content["authority"] == 100
    assert not store.memory.query(
        MemoryQuery(
            task_id=task.id,
            text="config MODE value",
            kinds=[MemoryKind.SEMANTIC],
            fact_types=["code_symbol"],
            epistemic_statuses=["inferred"],
        )
    ).hits
    history = store.memory.query(
        MemoryQuery(
            task_id=task.id,
            text="config MODE value",
            kinds=[MemoryKind.SEMANTIC],
            statuses=[MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED],
            paths=["config.py"],
            fact_types=["code_symbol"],
        )
    )
    assert len(history.hits) == 3
    validate_supersession_chain([hit.record for hit in history.hits])
    assert result.report is not None
    assert result.report.semantic_facts_superseded == 2
    assert result.report.semantic_facts_created >= 6
    metrics = TaskMetrics.from_events(task.id, trace.read())
    assert metrics.semantic_facts_superseded == 2
    assert metrics.semantic_facts_created == result.report.semantic_facts_created
