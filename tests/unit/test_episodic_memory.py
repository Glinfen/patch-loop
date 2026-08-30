from __future__ import annotations

from patchloop.domain import ErrorKind, Plan, PlanItem, StepStatus, ToolCall, ToolResult
from patchloop.memory import (
    EPISODIC_MEMORY_PREFIX,
    EpisodeOutcome,
    EpisodicMemoryManager,
)


def _result(
    call: ToolCall,
    *,
    success: bool,
    output: str,
    error_kind: ErrorKind | None = None,
) -> ToolResult:
    return ToolResult(
        call_id=call.id,
        tool_name=call.name,
        success=success,
        output=output,
        error_kind=error_kind,
    )


def _plan() -> Plan:
    return Plan(
        items=[
            PlanItem(
                id="fix-parser",
                description="Fix parser recovery",
                status=StepStatus.RUNNING,
            )
        ]
    )


def test_failed_action_and_corrected_action_form_a_causal_recovery() -> None:
    memory = EpisodicMemoryManager("task-episode", "Fix parser")
    failed = ToolCall(
        id="failed-write",
        name="apply_patch",
        arguments={"path": "src/parser.py", "edits": [{"old_text": "bad"}]},
    )
    failure = memory.observe_tool(
        failed,
        _result(
            failed,
            success=False,
            output="old text not found",
            error_kind=ErrorKind.EXECUTION_ERROR,
        ),
        step_index=2,
        plan=_plan(),
        changed_paths=[],
    )

    assert failure is not None
    assert failure.reference.outcome is EpisodeOutcome.FAILED
    assert failure.reference.error_kind is ErrorKind.EXECUTION_ERROR
    assert failure.reference.paths == ["src/parser.py"]
    assert failure.record.content["intent"] == "Fix parser recovery"
    assert failure.record.content["outcome"] == "failed"
    assert failure.record.content["error_kind"] == "execution_error"
    assert failure.record.content["paths"] == ["src/parser.py"]
    assert failure.record.content["actions"]
    assert failure.record.content["observations"] == ["old text not found"]
    assert memory.is_known_failed_action(failed)
    assert memory.render().startswith(EPISODIC_MEMORY_PREFIX)
    assert "old text not found" in memory.render()

    corrected = ToolCall(
        id="corrected-write",
        name="apply_patch",
        arguments={
            "path": "src/parser.py",
            "edits": [{"old_text": "actual", "new_text": "fixed"}],
        },
    )
    recovery = memory.observe_tool(
        corrected,
        _result(corrected, success=True, output="patch applied"),
        step_index=4,
        plan=_plan(),
        changed_paths=["src/parser.py"],
    )

    assert recovery is not None
    assert recovery.reference.outcome is EpisodeOutcome.RECOVERED
    assert recovery.reference.recovers_episode_ids == [failure.reference.id]
    assert memory.snapshot().unresolved_failures == []
    assert memory.snapshot().recovery_count == 1
    assert len(recovery.sources) == 1


def test_successful_verification_resolves_all_failures_and_marks_resume_anchor() -> None:
    memory = EpisodicMemoryManager("task-verified", "Repair and verify")
    failures = []
    for index, name in enumerate(("apply_patch", "read_file")):
        call = ToolCall(id=f"failure-{index}", name=name, arguments={"path": "src/a.py"})
        episode = memory.observe_tool(
            call,
            _result(
                call,
                success=False,
                output=f"failure {index}",
                error_kind=ErrorKind.EXECUTION_ERROR,
            ),
            step_index=index,
            plan=None,
            changed_paths=[],
        )
        assert episode is not None
        failures.append(episode.reference.id)

    tests = ToolCall(id="verified-tests", name="run_tests", arguments={"command": "pytest"})
    verified = memory.observe_tool(
        tests,
        _result(tests, success=True, output="12 passed"),
        step_index=3,
        plan=None,
        changed_paths=["src/a.py"],
    )

    assert verified is not None
    assert verified.reference.outcome is EpisodeOutcome.VERIFIED
    assert verified.reference.recovers_episode_ids == failures
    snapshot = memory.snapshot()
    assert snapshot.last_verified_episode_id == verified.reference.id
    assert snapshot.unresolved_failures == []
    assert verified.reference.id in memory.render()


def test_checkpoint_and_tool_observation_are_idempotent_across_snapshot_restore() -> None:
    memory = EpisodicMemoryManager("task-checkpoint", "Inspect repository")
    call = ToolCall(id="read-once", name="read_file", arguments={"path": "src/a.py"})
    write = memory.observe_tool(
        call,
        _result(call, success=True, output="contents"),
        step_index=0,
        plan=None,
        changed_paths=[],
    )
    checkpoint = memory.observe_checkpoint(
        step_index=1,
        plan=None,
        changed_paths=["src/a.py", "tests/test_a.py"],
    )

    assert write is not None
    assert checkpoint is not None
    assert checkpoint.reference.outcome is EpisodeOutcome.CHECKPOINTED
    assert len(checkpoint.sources) == 2
    snapshot = memory.snapshot()
    restored = EpisodicMemoryManager(
        "task-checkpoint",
        "Inspect repository",
        snapshot=snapshot,
    )

    assert restored.snapshot() == snapshot
    assert (
        restored.observe_tool(
            call,
            _result(call, success=True, output="contents"),
            step_index=0,
            plan=None,
            changed_paths=[],
        )
        is None
    )
    assert restored.observe_checkpoint(step_index=1, plan=None, changed_paths=[]) is None


def test_manager_can_rebuild_recovery_state_from_persisted_episode_records() -> None:
    memory = EpisodicMemoryManager("task-rebuild", "Fix parser")
    failed = ToolCall(id="failed", name="apply_patch", arguments={"path": "src/a.py"})
    failed_write = memory.observe_tool(
        failed,
        _result(
            failed,
            success=False,
            output="bad edit",
            error_kind=ErrorKind.EXECUTION_ERROR,
        ),
        step_index=0,
        plan=None,
        changed_paths=[],
    )
    corrected = ToolCall(
        id="corrected",
        name="apply_patch",
        arguments={"path": "src/a.py", "edits": [{"old_text": "a", "new_text": "b"}]},
    )
    corrected_write = memory.observe_tool(
        corrected,
        _result(corrected, success=True, output="fixed"),
        step_index=1,
        plan=None,
        changed_paths=["src/a.py"],
    )
    assert failed_write is not None
    assert corrected_write is not None

    rebuilt = EpisodicMemoryManager(
        "task-rebuild",
        "Fix parser",
        records=[failed_write.record, corrected_write.record],
    )

    assert rebuilt.snapshot().episode_count == 2
    assert rebuilt.snapshot().recovery_count == 1
    assert rebuilt.snapshot().unresolved_failures == []
    assert "latest_recovery" in rebuilt.render()
