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
