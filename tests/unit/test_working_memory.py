from __future__ import annotations

import json

import pytest

from patchloop.domain import (
    ErrorKind,
    Plan,
    PlanItem,
    StepStatus,
    ToolCall,
    ToolResult,
)
from patchloop.memory import (
    WORKING_MEMORY_PREFIX,
    MemoryKind,
    WorkingMemoryBudgetError,
    WorkingMemoryItemKind,
    WorkingMemoryManager,
)


def _result(
    call: ToolCall,
    output: str,
    *,
    success: bool = True,
    error_kind: ErrorKind | None = None,
) -> ToolResult:
    return ToolResult(
        call_id=call.id,
        tool_name=call.name,
        success=success,
        output=output,
        error_kind=error_kind,
    )


def test_extracts_and_pins_goal_constraints_and_prohibitions() -> None:
    memory = WorkingMemoryManager(
        "task-working",
        "Only inspect src. Do not edit secrets. 必须保持 API 兼容。",
        token_budget=600,
    )

    snapshot = memory.snapshot()
    kinds = {item.kind for item in snapshot.items}

    assert WorkingMemoryItemKind.GOAL in kinds
    assert WorkingMemoryItemKind.CONSTRAINT in kinds
    assert WorkingMemoryItemKind.PROHIBITION in kinds
    assert all(item.pinned for item in snapshot.items)
    assert memory.render().startswith(WORKING_MEMORY_PREFIX)
    assert snapshot.estimated_tokens <= snapshot.token_budget


def test_100_steps_remain_bounded_and_keep_constraints_and_active_error() -> None:
    memory = WorkingMemoryManager(
        "task-long",
        "Only inspect the repository and do not modify .env.",
        token_budget=650,
    )

    for step in range(100):
        if step == 50:
            call = ToolCall(id="error-50", name="missing_tool")
            result = _result(
                call,
                "missing tool failure with diagnostic " + "x" * 500,
                success=False,
                error_kind=ErrorKind.UNKNOWN_TOOL,
            )
        else:
            call = ToolCall(id=f"read-{step}", name="read_file")
            result = _result(call, f"line {step} " + "payload " * 80)
        memory.observe_tool(
            call,
            result,
            step_index=step,
            plan=None,
            changed_paths=[f"src/file_{step % 7}.py"],
        )

    snapshot = memory.snapshot()
    rendered = memory.render()

    assert snapshot.estimated_tokens <= 650
    assert snapshot.max_estimated_tokens <= 650
    assert snapshot.evicted_count > 0
    assert any(item.kind is WorkingMemoryItemKind.PROHIBITION for item in snapshot.items)
    assert any(item.kind is WorkingMemoryItemKind.ACTIVE_ERROR for item in snapshot.items)
    assert rendered.count("read_file passed") <= 1
    assert "line 99" in rendered
    assert "line 1 payload" not in rendered


def test_error_and_question_are_explicitly_evicted_when_resolved() -> None:
    memory = WorkingMemoryManager("task-resolve", "Inspect the bug", token_budget=500)
    memory.add_open_question("scope", "Which parser owns this input?", step_index=1)
    failed = ToolCall(id="failed", name="read_file")
    memory.observe_tool(
        failed,
        _result(failed, "not found", success=False, error_kind=ErrorKind.EXECUTION_ERROR),
        step_index=2,
        plan=None,
        changed_paths=[],
    )

    succeeded = ToolCall(id="succeeded", name="read_file")
    memory.observe_tool(
        succeeded,
        _result(succeeded, "found parser"),
        step_index=3,
        plan=None,
        changed_paths=[],
    )
    memory.resolve_open_question("scope")

    keys = {item.key for item in memory.snapshot().items}
    assert "error:read_file" not in keys
    assert "question:scope" not in keys


def test_completed_phase_and_verification_are_promoted_once() -> None:
    memory = WorkingMemoryManager("task-promote", "Implement parser", token_budget=900)
    running = Plan(
        items=[PlanItem(id="parser", description="Implement parser", status=StepStatus.RUNNING)]
    )
    update_running = ToolCall(id="plan-running", name="update_plan")
    memory.observe_tool(
        update_running,
        _result(update_running, "plan running"),
        step_index=0,
        plan=running,
        changed_paths=[],
    )
    completed = Plan(
        revision=2,
        items=[
            PlanItem(
                id="parser",
                description="Implement parser",
                status=StepStatus.COMPLETED,
                evidence=["tests/test_parser.py passed"],
            )
        ],
    )
    update_completed = ToolCall(id="plan-completed", name="update_plan")
    promotion = memory.observe_tool(
        update_completed,
        _result(update_completed, "plan completed"),
        step_index=1,
        plan=completed,
        changed_paths=["src/parser.py"],
    )

    assert {record.kind for record in promotion.records} == {
        MemoryKind.SEMANTIC,
        MemoryKind.EPISODIC,
    }
    assert len(promotion.sources) == 1
    replay = memory.observe_tool(
        update_completed,
        _result(update_completed, "plan completed"),
        step_index=1,
        plan=completed,
        changed_paths=["src/parser.py"],
    )
    assert replay.records == ()

    tests = ToolCall(id="tests", name="run_tests")
    verification = memory.observe_tool(
        tests,
        _result(tests, json.dumps({"exit_code": 0, "output": "12 passed"})),
        step_index=2,
        plan=completed,
        changed_paths=["src/parser.py"],
    )
    assert len(verification.records) == 1
    assert verification.records[0].kind is MemoryKind.SEMANTIC


def test_snapshot_round_trip_restores_the_same_context() -> None:
    memory = WorkingMemoryManager("task-resume", "Only inspect src", token_budget=500)
    call = ToolCall(id="read", name="read_file")
    memory.observe_tool(
        call,
        _result(call, "important result"),
        step_index=4,
        plan=None,
        changed_paths=["src/main.py"],
    )
    snapshot = memory.snapshot()

    restored = WorkingMemoryManager(
        "task-resume",
        "ignored during restore",
        token_budget=500,
        snapshot=snapshot,
    )

    assert restored.snapshot() == snapshot
    assert restored.render() == memory.render()


def test_successful_file_reads_keep_a_compact_completed_discovery_index() -> None:
    memory = WorkingMemoryManager(
        "task-read-progress",
        "Read evidence files once and then implement the change",
        token_budget=650,
    )

    for index in range(12):
        path = f"evidence/{index + 1:02d}_note.md"
        call = ToolCall(id=f"read-{index}", name="read_file", arguments={"path": path})
        memory.observe_tool(
            call,
            _result(call, "repository evidence " + "x" * 300),
            step_index=index,
            plan=None,
            changed_paths=[],
        )

    snapshot = memory.snapshot()
    read_paths = {
        item.text for item in snapshot.items if item.kind is WorkingMemoryItemKind.ACCESSED_FILE
    }

    assert read_paths == {f"evidence/{index + 1:02d}_note.md" for index in range(12)}
    assert snapshot.estimated_tokens <= snapshot.token_budget
    assert '"read_files"' in memory.render()


def test_file_write_invalidates_only_matching_read_signatures() -> None:
    memory = WorkingMemoryManager("task-read-invalidation", "Update source", token_budget=650)
    source_read = ToolCall(id="read-source", name="read_file", arguments={"path": "src/a.py"})
    test_read = ToolCall(id="read-test", name="read_file", arguments={"path": "tests/test_a.py"})
    for step, call in enumerate((source_read, test_read)):
        memory.observe_tool(
            call,
            _result(call, "contents"),
            step_index=step,
            plan=None,
            changed_paths=[],
        )

    write = ToolCall(id="write-source", name="write_file", arguments={"path": "src/a.py"})
    memory.observe_tool(
        write,
        _result(write, "wrote src/a.py"),
        step_index=2,
        plan=None,
        changed_paths=["src/a.py"],
    )

    assert not memory.has_read_call(source_read)
    assert memory.has_read_call(test_read)
    assert "src/a.py" not in {
        item.text
        for item in memory.snapshot().items
        if item.kind is WorkingMemoryItemKind.ACCESSED_FILE
    }


def test_mandatory_items_fail_closed_when_they_exceed_budget() -> None:
    with pytest.raises(WorkingMemoryBudgetError, match="mandatory working memory"):
        WorkingMemoryManager(
            "task-overflow",
            "Only " + "retain this mandatory constraint " * 80,
            token_budget=128,
        )


def test_working_memory_redacts_secrets_before_state_and_source_hashing() -> None:
    secret = "sk-" + "a" * 32
    memory = WorkingMemoryManager(
        "task-secret",
        f"Do not expose {secret}",
        token_budget=500,
    )
    call = ToolCall(id="secret-result", name="read_file")
    promotion = memory.observe_tool(
        call,
        _result(call, f"provider key is {secret}"),
        step_index=1,
        plan=None,
        changed_paths=[],
    )

    serialized = memory.snapshot().model_dump_json()
    assert secret not in serialized
    assert "[REDACTED]" in serialized
    assert promotion.records == ()
