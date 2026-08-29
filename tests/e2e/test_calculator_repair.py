import shutil
from pathlib import Path

from patchloop.domain import Task, TaskStatus, ToolCall
from patchloop.events import EventLogger
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.runtime import AgentRuntime
from patchloop.storage import ArtifactStore
from patchloop.tools import (
    ApplyPatchTool,
    GetDiffTool,
    PermissionLevel,
    ReadFileTool,
    RunTestsTool,
    ToolContext,
    ToolGateway,
    ToolPolicy,
    UpdatePlanTool,
)


def test_agent_repairs_calculator_fixture_and_runs_tests(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "benchmarks" / "fixtures" / "calculator_bug"
    repository = tmp_path / "calculator_bug"
    shutil.copytree(source, repository)
    trace = EventLogger(tmp_path / "trace.jsonl")
    gateway = ToolGateway(
        ToolContext(repository),
        [
            ReadFileTool(),
            UpdatePlanTool(),
            ApplyPatchTool(),
            RunTestsTool(),
            GetDiffTool(),
        ],
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
                        name="update_plan",
                        arguments={
                            "items": [
                                {"description": "Inspect the defect", "status": "completed"},
                                {"description": "Fix division", "status": "running"},
                                {"description": "Run regression test", "status": "pending"},
                            ]
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        name="apply_patch",
                        arguments={
                            "path": "calculator.py",
                            "edits": [
                                {
                                    "old_text": "return dividend // divisor",
                                    "new_text": "return dividend / divisor",
                                }
                            ],
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
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        name="update_plan",
                        arguments={
                            "items": [
                                {
                                    "description": "Inspect the defect",
                                    "status": "completed",
                                    "evidence": ["calculator.py:3"],
                                },
                                {
                                    "description": "Fix division",
                                    "status": "completed",
                                    "evidence": ["calculator.py changed"],
                                },
                                {
                                    "description": "Run regression test",
                                    "status": "completed",
                                    "evidence": ["1 passed"],
                                },
                            ]
                        },
                    )
                ]
            ),
            ModelResponse(content="Fixed integer floor division and verified the regression test."),
        ]
    )
    task = Task(goal="Fix divide and run tests", repository=str(repository))

    result = AgentRuntime(provider, gateway, trace).run(task)
    artifacts = ArtifactStore(tmp_path / "artifacts").save_report(result)

    assert result.status is TaskStatus.COMPLETED
    assert "dividend / divisor" in (repository / "calculator.py").read_text(encoding="utf-8")
    assert result.plan is not None
    assert all(item.status.value == "completed" for item in result.plan.items)
    assert result.report is not None
    assert result.report.changed_files == ["calculator.py"]
    assert result.report.validations[0].passed
    assert {path.name for path in artifacts} == {"report.json", "changes.diff"}
    tool_events = [event for event in trace.read() if event.type == "tool.completed"]
    test_event = next(event for event in tool_events if event.data["call"]["name"] == "run_tests")
    test_result = test_event.data["result"]
    assert isinstance(test_result, dict)
    assert '"exit_code": 0' in str(test_result["output"])
    diff_event = next(event for event in tool_events if event.data["call"]["name"] == "get_diff")
    assert "+    return dividend / divisor" in str(diff_event.data["result"])
