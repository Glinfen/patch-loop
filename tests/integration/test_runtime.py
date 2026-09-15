from pathlib import Path

import pytest

from patchloop.domain import Task, TaskBudget, TaskStatus, ToolCall
from patchloop.events import EventLogger
from patchloop.providers import FakeProvider, ModelResponse, ModelUsage
from patchloop.runtime import AgentRuntime
from patchloop.tools import (
    ListFilesTool,
    ReadFileTool,
    SearchTextTool,
    ToolContext,
    ToolGateway,
    UpdatePlanTool,
)


def test_runtime_executes_multiple_tools_and_records_trace(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("VALUE = 42\n", encoding="utf-8")
    trace = EventLogger(tmp_path / "trace.jsonl")
    gateway = ToolGateway(
        ToolContext(repository),
        [ListFilesTool(), ReadFileTool(), SearchTextTool()],
        trace,
    )
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=[ToolCall(name="list_files")]),
            ModelResponse(tool_calls=[ToolCall(name="read_file", arguments={"path": "app.py"})]),
            ModelResponse(content="The repository defines VALUE in app.py:1."),
        ]
    )
    runtime = AgentRuntime(provider, gateway, trace)
    task = Task(
        goal="Find where VALUE is defined",
        repository=str(repository),
        budget=TaskBudget(max_steps=5),
    )

    result = runtime.run(task)

    assert result.status is TaskStatus.COMPLETED
    assert result.result == "The repository defines VALUE in app.py:1."
    assert result.report is not None
    assert result.report.tool_calls == 2
    assert result.report.changed_files == []
    assert len(provider.requests) == 3
    events = trace.read()
    assert [event.type for event in events].count("tool.completed") == 2
    assert events[0].type == "task.started"
    assert events[-1].type == "task.completed"


def test_runtime_reports_and_traces_provider_cache_usage(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    trace = EventLogger(tmp_path / "trace.jsonl")
    gateway = ToolGateway(ToolContext(repository), [ListFilesTool()], trace)
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(name="list_files")],
                usage=ModelUsage(
                    input_tokens=10,
                    output_tokens=2,
                    cost_usd=0.01,
                    cache_hit_tokens=10,
                    cache_miss_tokens=0,
                    cache_write_tokens=3,
                ),
            ),
            ModelResponse(
                content="Done",
                usage=ModelUsage(
                    input_tokens=20,
                    output_tokens=4,
                    cost_usd=0.02,
                    cache_hit_tokens=0,
                    cache_miss_tokens=20,
                ),
            ),
        ]
    )

    result = AgentRuntime(provider, gateway, trace).run(
        Task(goal="Measure cache usage", repository=str(repository))
    )

    assert result.status is TaskStatus.COMPLETED
    assert result.report is not None
    assert result.report.input_tokens == 30
    assert result.report.cache_hit_tokens == 10
    assert result.report.cache_miss_tokens == 20
    assert result.report.cache_write_tokens == 3
    assert result.report.cache_hit_rate == 1 / 3
    assert result.report.cache_usage_reported_calls == 2
    assert result.report.cache_usage_unreported_calls == 0
    assert result.report.cache_usage_inconsistent_calls == 0
    assert result.report.cache_write_reported_calls == 1
    assert result.report.provider_requests == 2
    assert result.report.provider_attempts == 2
    assert result.report.unknown_usage_attempts == 0
    assert result.report.reserved_cost_usd == 0
    model_events = [event for event in trace.read() if event.type == "model.completed"]
    assert model_events[0].data["usage"]["cache_hit_tokens"] == 10
    assert model_events[1].data["usage"]["cache_miss_tokens"] == 20


def test_runtime_stops_at_step_budget(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    gateway = ToolGateway(ToolContext(repository), [ListFilesTool()])
    provider = FakeProvider(
        [ModelResponse(tool_calls=[ToolCall(name="list_files")]) for _ in range(2)]
    )
    runtime = AgentRuntime(provider, gateway)
    task = Task(
        goal="Keep looking forever",
        repository=str(repository),
        budget=TaskBudget(max_steps=2),
    )

    result = runtime.run(task)

    assert result.status is TaskStatus.FAILED
    assert result.error == "step budget exceeded (2)"
    assert result.report is not None


def test_runtime_stops_repeated_action_loop(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    gateway = ToolGateway(ToolContext(repository), [ListFilesTool()])
    provider = FakeProvider(
        [ModelResponse(tool_calls=[ToolCall(name="list_files")]) for _ in range(3)]
    )
    runtime = AgentRuntime(provider, gateway)
    task = Task(
        goal="Loop",
        repository=str(repository),
        budget=TaskBudget(max_steps=5, max_repeated_actions=2),
    )

    result = runtime.run(task)

    assert result.status is TaskStatus.FAILED
    assert result.error == "identical action repeated 2 times"


def test_runtime_recovers_after_invalid_tool_call(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    trace = EventLogger(tmp_path / "trace.jsonl")
    gateway = ToolGateway(ToolContext(repository), [ListFilesTool(), ReadFileTool()], trace)
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=[ToolCall(name="read_file", arguments={})]),
            ModelResponse(tool_calls=[ToolCall(name="list_files")]),
            ModelResponse(content="Recovered from the invalid call."),
        ]
    )

    result = AgentRuntime(provider, gateway, trace).run(
        Task(goal="Recover", repository=str(repository))
    )

    assert result.status is TaskStatus.COMPLETED
    tool_events = [event for event in trace.read() if event.type == "tool.completed"]
    first_result = tool_events[0].data["result"]
    assert isinstance(first_result, dict)
    assert first_result["error_kind"] == "invalid_arguments"


def test_runtime_skips_duplicate_read_of_an_unchanged_file(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("VALUE = 42\n", encoding="utf-8")
    trace = EventLogger(tmp_path / "trace.jsonl")
    gateway = ToolGateway(ToolContext(repository), [ReadFileTool()], trace)
    repeated_read = {"path": "app.py"}
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=[ToolCall(name="read_file", arguments=repeated_read)]),
            ModelResponse(tool_calls=[ToolCall(name="read_file", arguments=repeated_read)]),
            ModelResponse(content="Used the earlier observation."),
        ]
    )

    result = AgentRuntime(provider, gateway, trace).run(
        Task(goal="Read app.py without duplicate work", repository=str(repository))
    )

    assert result.status is TaskStatus.COMPLETED
    assert gateway.history[0].output.startswith("1: VALUE = 42")
    assert gateway.history[1].output.startswith("Skipped duplicate read")
    skipped = [event for event in trace.read() if event.type == "tool.duplicate_read_skipped"]
    assert len(skipped) == 1


@pytest.mark.parametrize(
    ("usage", "budget", "expected_error"),
    [
        (
            ModelUsage(input_tokens=51),
            TaskBudget(max_input_tokens=50),
            "input token budget exceeded (50)",
        ),
        (
            ModelUsage(output_tokens=21),
            TaskBudget(max_output_tokens=20),
            "output token budget exceeded (20)",
        ),
        (
            ModelUsage(cost_usd=0.11),
            TaskBudget(max_cost_usd=0.1),
            "cost budget exceeded ($0.1000)",
        ),
    ],
)
def test_runtime_enforces_model_budgets(
    tmp_path: Path,
    usage: ModelUsage,
    budget: TaskBudget,
    expected_error: str,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    gateway = ToolGateway(ToolContext(repository), [ListFilesTool()])
    provider = FakeProvider([ModelResponse(content="Done", usage=usage)])

    result = AgentRuntime(provider, gateway).run(
        Task(goal="Budget test", repository=str(repository), budget=budget)
    )

    assert result.status is TaskStatus.FAILED
    assert result.error == expected_error
    assert result.report is not None


def test_runtime_stops_repeated_tool_error(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    gateway = ToolGateway(ToolContext(repository), [ListFilesTool()])
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=[ToolCall(name="missing", arguments={"attempt": 1})]),
            ModelResponse(tool_calls=[ToolCall(name="missing", arguments={"attempt": 2})]),
        ]
    )
    budget = TaskBudget(max_repeated_actions=10, max_repeated_errors=2)

    result = AgentRuntime(provider, gateway).run(
        Task(goal="Repeat errors", repository=str(repository), budget=budget)
    )

    assert result.status is TaskStatus.FAILED
    assert result.error == "the same tool error repeated 2 times"


def test_runtime_enforces_tool_failure_budget(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    gateway = ToolGateway(ToolContext(repository), [ListFilesTool()])
    provider = FakeProvider([ModelResponse(tool_calls=[ToolCall(name="missing")])])

    result = AgentRuntime(provider, gateway).run(
        Task(
            goal="Failure budget",
            repository=str(repository),
            budget=TaskBudget(max_tool_failures=0),
        )
    )

    assert result.status is TaskStatus.FAILED
    assert result.error == "tool failure budget exceeded (0)"


def test_runtime_enforces_replan_budget(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    context = ToolContext(repository)
    context.requires_replan = True
    gateway = ToolGateway(context, [UpdatePlanTool()])
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        name="update_plan",
                        arguments={"items": [{"description": "Recover", "status": "running"}]},
                    )
                ]
            )
        ]
    )

    result = AgentRuntime(provider, gateway).run(
        Task(
            goal="Replan budget",
            repository=str(repository),
            budget=TaskBudget(max_replans=0),
        )
    )

    assert result.status is TaskStatus.FAILED
    assert result.error == "replan budget exceeded (0)"
