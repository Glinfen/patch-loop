import shutil
from pathlib import Path

from patchloop.domain import Task, TaskStatus, ToolCall
from patchloop.events import EventLogger
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.runtime import AgentRuntime
from patchloop.tools import (
    GetDiffTool,
    PermissionLevel,
    ReadFileTool,
    ReplaceTextTool,
    RunTestsTool,
    ToolContext,
    ToolGateway,
    ToolPolicy,
)


def test_agent_repairs_calculator_fixture_and_runs_tests(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "benchmarks" / "fixtures" / "calculator_bug"
    repository = tmp_path / "calculator_bug"
    shutil.copytree(source, repository)
    trace = EventLogger(tmp_path / "trace.jsonl")
    gateway = ToolGateway(
        ToolContext(repository),
        [ReadFileTool(), ReplaceTextTool(), RunTestsTool(), GetDiffTool()],
        trace,
        ToolPolicy(
            frozenset({PermissionLevel.READ, PermissionLevel.WRITE, PermissionLevel.EXECUTE})
        ),
    )
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(name="read_file", arguments={"path": "calculator.py"})]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        name="replace_text",
                        arguments={
                            "path": "calculator.py",
                            "old_text": "return dividend // divisor",
                            "new_text": "return dividend / divisor",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
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
            ModelResponse(tool_calls=[ToolCall(name="get_diff")]),
            ModelResponse(content="Fixed integer floor division and verified the regression test."),
        ]
    )
    task = Task(goal="Fix divide and run tests", repository=str(repository))

    result = AgentRuntime(provider, gateway, trace).run(task)

    assert result.status is TaskStatus.COMPLETED
    assert "dividend / divisor" in (repository / "calculator.py").read_text(encoding="utf-8")
    tool_events = [event for event in trace.read() if event.type == "tool.completed"]
    test_result = tool_events[2].data["result"]
    assert isinstance(test_result, dict)
    assert '"exit_code": 0' in str(test_result["output"])
    assert "+    return dividend / divisor" in str(tool_events[3].data["result"])
