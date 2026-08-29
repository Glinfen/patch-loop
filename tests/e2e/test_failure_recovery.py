import shutil
from pathlib import Path

from patchloop.domain import ErrorKind, Task, TaskStatus, ToolCall
from patchloop.events import EventLogger
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.runtime import AgentRuntime
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


def test_agent_replans_and_recovers_from_failed_patch(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "benchmarks" / "fixtures" / "calculator_bug"
    repository = tmp_path / "calculator_bug"
    shutil.copytree(source, repository)
    trace = EventLogger(tmp_path / "trace.jsonl")
    gateway = ToolGateway(
        ToolContext(repository),
        [ReadFileTool(), UpdatePlanTool(), ApplyPatchTool(), RunTestsTool()],
        trace,
        ToolPolicy(
            frozenset({PermissionLevel.READ, PermissionLevel.WRITE, PermissionLevel.EXECUTE})
        ),
    )
    test_arguments = {"command": ["python", "-m", "pytest", "-q", "test_calculator.py"]}
    correct_patch_arguments = {
        "path": "calculator.py",
        "edits": [
            {
                "old_text": "return dividend + divisor",
                "new_text": "return dividend / divisor",
            }
        ],
    }
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
                                {"description": "Attempt repair", "status": "running"},
                                {"description": "Verify", "status": "pending"},
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
                                    "new_text": "return dividend + divisor",
                                }
                            ],
                        },
                    )
                ]
            ),
            ModelResponse(tool_calls=[ToolCall(name="run_tests", arguments=test_arguments)]),
            # This premature write must be denied until the failed test is reflected in a plan.
            ModelResponse(
                tool_calls=[ToolCall(name="apply_patch", arguments=correct_patch_arguments)]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        name="update_plan",
                        arguments={
                            "items": [
                                {
                                    "description": "Correct failed repair",
                                    "status": "running",
                                    "evidence": ["pytest expected 2.5 but received 7"],
                                },
                                {"description": "Verify again", "status": "pending"},
                            ]
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[ToolCall(name="apply_patch", arguments=correct_patch_arguments)]
            ),
            ModelResponse(tool_calls=[ToolCall(name="run_tests", arguments=test_arguments)]),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        name="update_plan",
                        arguments={
                            "items": [
                                {
                                    "description": "Correct failed repair",
                                    "status": "completed",
                                    "evidence": ["calculator.py uses true division"],
                                },
                                {
                                    "description": "Verify again",
                                    "status": "completed",
                                    "evidence": ["1 passed"],
                                },
                            ]
                        },
                    )
                ]
            ),
            ModelResponse(content="Recovered from the failed attempt and verified the fix."),
        ]
    )

    result = AgentRuntime(provider, gateway, trace).run(
        Task(goal="Repair divide with recovery", repository=str(repository))
    )

    assert result.status is TaskStatus.COMPLETED
    assert result.plan is not None and result.plan.revision == 3
    assert result.report is not None
    assert result.report.replans == 1
    assert [record.passed for record in result.report.validations] == [False, True]
    assert result.report.validations[0].error_kind is ErrorKind.TEST_FAILURE
    assert "return dividend / divisor" in (repository / "calculator.py").read_text(encoding="utf-8")
    events = trace.read()
    failures = [
        event
        for event in events
        if event.type == "tool.completed" and event.data["result"]["error_kind"] == "test_failure"
    ]
    assert len(failures) == 1
