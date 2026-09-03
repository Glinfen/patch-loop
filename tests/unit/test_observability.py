from patchloop.events import Event
from patchloop.observability import TaskMetrics, TaskReplay


def test_metrics_and_replay_locate_failed_tool_step() -> None:
    events = [
        Event(type="task.started", task_id="task-1", sequence=1),
        Event(type="step.started", task_id="task-1", sequence=2, data={"step": 0}),
        Event(
            type="model.completed",
            task_id="task-1",
            sequence=3,
            data={"step": 0, "usage": {"input_tokens": 12, "output_tokens": 3, "cost_usd": 0.1}},
        ),
        Event(
            type="security.decision",
            task_id="task-1",
            sequence=4,
            data={
                "step": 0,
                "assessment": {
                    "risk": "high",
                    "allowed": False,
                    "approval_required": True,
                    "reason": "operator approval was not granted",
                },
            },
        ),
        Event(
            type="tool.completed",
            task_id="task-1",
            sequence=5,
            data={
                "result": {
                    "tool_name": "run_tests",
                    "success": False,
                    "error_kind": "permission_denied",
                    "duration_ms": 2.5,
                }
            },
        ),
        Event(
            type="task.failed",
            task_id="task-1",
            sequence=6,
            data={"error_kind": "budget_exceeded"},
        ),
    ]

    metrics = TaskMetrics.from_events("task-1", events)
    replay = TaskReplay.from_events("task-1", events)

    assert metrics.status == "failed"
    assert metrics.steps == 1 and metrics.model_calls == 1
    assert metrics.tool_calls == 1 and metrics.failed_tool_calls == 1
    assert metrics.approvals_requested == 1 and metrics.approvals_denied == 1
    assert metrics.input_tokens == 12 and metrics.output_tokens == 3
    assert metrics.cost_usd == 0.1 and metrics.tool_duration_ms == 2.5
    assert metrics.errors == {"budget_exceeded": 1, "permission_denied": 1}
    assert replay.frames[4].sequence == 5
    assert replay.frames[4].summary == "run_tests failed"


def test_memory_metrics_and_replay_explain_model_memory_decision() -> None:
    events = [
        Event(type="task.started", task_id="task-memory", sequence=1),
        Event(
            type="memory.written",
            task_id="task-memory",
            sequence=2,
            data={
                "record_ids": ["record-1", "record-old"],
                "write_duration_ms": 2.5,
                "inventory": {
                    "by_kind": {"semantic": 2},
                    "by_status": {"active": 1, "superseded": 1},
                },
            },
        ),
        Event(type="step.started", task_id="task-memory", sequence=3, data={"step": 4}),
        Event(
            type="memory.security_filtered",
            task_id="task-memory",
            sequence=4,
            data={
                "step": 4,
                "selections": [
                    {
                        "record_id": "record-1",
                        "findings": ["credential_redacted", "prompt_injection_blocked"],
                    }
                ],
            },
        ),
        Event(
            type="memory.retrieved",
            task_id="task-memory",
            sequence=5,
            data={
                "step": 4,
                "query": "payment contract",
                "estimated_tokens": 120,
                "context_occupancy": 0.6,
                "read_duration_ms": 1.5,
                "stale_hits": 0,
                "selected": [
                    {
                        "record_id": "record-1",
                        "status": "active",
                        "source_ids": ["source-1"],
                        "score": 0.9,
                        "score_components": {"lexical": 1.0},
                        "reason": "hybrid: lexical=1.000",
                    }
                ],
                "omitted_ids": ["record:record-old"],
            },
        ),
        Event(
            type="memory.superseded",
            task_id="task-memory",
            sequence=6,
            data={"records": [{"record_id": "record-old"}]},
        ),
        Event(
            type="memory.compacted",
            task_id="task-memory",
            sequence=7,
            data={
                "written_record_ids": ["summary-1"],
                "input_tokens": 80,
                "output_tokens": 40,
                "duration_ms": 3.0,
                "inventory": {
                    "by_kind": {"semantic": 3},
                    "by_status": {"active": 2, "superseded": 1},
                },
            },
        ),
        Event(
            type="memory.replayed",
            task_id="task-memory",
            sequence=8,
            data={"step": 4, "event_id": "tool:read", "writes_suppressed": True},
        ),
        Event(type="task.completed", task_id="task-memory", sequence=9),
    ]

    metrics = TaskMetrics.from_events("task-memory", events)
    replay = TaskReplay.from_events("task-memory", events)

    assert metrics.memory_records_written == 3
    assert metrics.memory_records_superseded == 1
    assert metrics.memory_compactions == 1
    assert metrics.memory_replays == 1
    assert metrics.memory_security_filters == 2
    assert metrics.memory_read_duration_ms == 1.5
    assert metrics.memory_write_duration_ms == 2.5
    assert metrics.memory_compression_duration_ms == 3.0
    assert metrics.memory_compression_ratio == 0.5
    assert metrics.max_memory_context_tokens_used == 120
    assert metrics.max_memory_context_occupancy == 0.6
    assert metrics.memory_records_by_kind == {"semantic": 3}
    assert metrics.memory_records_by_status == {"active": 2, "superseded": 1}
    assert len(replay.memory_decisions) == 1
    decision = replay.memory_decisions[0]
    assert decision.step == 4
    assert decision.selected_record_ids == ["record-1"]
    assert decision.selections[0]["source_ids"] == ["source-1"]
    assert decision.selections[0]["reason"] == "hybrid: lexical=1.000"
