from pathlib import Path

from patchloop.domain import Task, TaskBudget, TaskStatus, ToolCall
from patchloop.events import EventLogger
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.runtime import AgentRuntime
from patchloop.tools import ListFilesTool, ReadFileTool, SearchTextTool, ToolContext, ToolGateway


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
