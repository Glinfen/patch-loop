from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from patchloop.domain import ErrorKind, Task, TaskStatus, ToolCall
from patchloop.events import EventLogger
from patchloop.memory import MemoryKind, MemoryQuery
from patchloop.observability import TaskMetrics
from patchloop.persistence import SQLiteStore
from patchloop.providers import ModelMessage, ModelResponse, ToolSpec
from patchloop.runtime import AgentRuntime
from patchloop.tools import (
    ApplyPatchTool,
    PermissionLevel,
    RunTestsTool,
    ToolContext,
    ToolGateway,
    ToolPolicy,
    UpdatePlanTool,
)


class EpisodeAwareProvider:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.index = 0
        self.saw_active_failure = False
        self.saw_recovery = False
        self.saw_verified_anchor = False

    @property
    def name(self) -> str:
        return "episode-aware"

    def complete(
        self,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        del tools
        joined = "\n".join(message.content for message in messages)
        if "active_failures" in joined and "expected 1 occurrences" in joined:
            self.saw_active_failure = True
        if "latest_recovery" in joined:
            self.saw_recovery = True
        if "last_verified_episode_id" in joined:
            self.saw_verified_anchor = True
        response = self.responses[self.index]
        self.index += 1
        return response


class InterruptAfterResponses:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = iter(responses)

    @property
    def name(self) -> str:
        return "episode-interrupt"

    def complete(
        self,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        del messages, tools
        try:
            return next(self.responses)
        except StopIteration:
            raise KeyboardInterrupt("interrupt after verified checkpoint") from None


class ResumeAnchorProvider:
    def __init__(self, expected_episode_id: str) -> None:
        self.expected_episode_id = expected_episode_id
        self.saw_anchor = False

    @property
    def name(self) -> str:
        return "episode-resume"

    def complete(
        self,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        del tools
        joined = "\n".join(message.content for message in messages)
        self.saw_anchor = (
            "PATCHLOOP_EPISODIC_MEMORY_V1" in joined and self.expected_episode_id in joined
        )
        return ModelResponse(content="Resumed from the verified episode without another write.")


def _repository(tmp_path: Path) -> Path:
    source = Path(__file__).parents[2] / "benchmarks" / "fixtures" / "calculator_bug"
    repository = tmp_path / "calculator_bug"
    shutil.copytree(source, repository)
    return repository


def _gateway(repository: Path, trace: EventLogger) -> ToolGateway:
    return ToolGateway(
        ToolContext(repository),
        [UpdatePlanTool(), ApplyPatchTool(), RunTestsTool()],
        trace,
        ToolPolicy(
            frozenset({PermissionLevel.READ, PermissionLevel.WRITE, PermissionLevel.EXECUTE})
        ),
    )


def _plan_call(call_id: str, description: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        name="update_plan",
        arguments={"items": [{"description": description, "status": "running"}]},
    )


def test_failure_episode_blocks_exact_retry_and_links_corrected_recovery(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    trace = EventLogger(tmp_path / "trace.jsonl")
    store = SQLiteStore(tmp_path / "state.db")
    failed_arguments = {
        "path": "calculator.py",
        "edits": [
            {
                "old_text": "return dividend + divisor",
                "new_text": "return dividend / divisor",
            }
        ],
    }
    corrected_arguments = {
        "path": "calculator.py",
        "edits": [
            {
                "old_text": "return dividend // divisor",
                "new_text": "return dividend / divisor",
            }
        ],
    }
    provider = EpisodeAwareProvider(
        [
            ModelResponse(tool_calls=[_plan_call("plan-1", "Repair calculator")]),
            ModelResponse(
                tool_calls=[
                    ToolCall(id="failed-write", name="apply_patch", arguments=failed_arguments)
                ]
            ),
            ModelResponse(tool_calls=[_plan_call("plan-2", "Recover failed patch")]),
            ModelResponse(
                tool_calls=[
                    ToolCall(id="repeated-write", name="apply_patch", arguments=failed_arguments)
                ]
            ),
            ModelResponse(tool_calls=[_plan_call("plan-3", "Use inspected source text")]),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="corrected-write",
                        name="apply_patch",
                        arguments=corrected_arguments,
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="verified-tests",
                        name="run_tests",
                        arguments={
                            "command": ["python", "-m", "pytest", "-q", "test_calculator.py"]
                        },
                    )
                ]
            ),
            ModelResponse(content="Recovered and verified the calculator repair."),
        ]
    )
    task = Task(
        id="episode-failure-recovery",
        goal="Repair calculator and avoid repeating failed edits",
        repository=str(repository),
    )

    result = AgentRuntime(provider, _gateway(repository, trace), trace, store).run(task)

    assert result.status is TaskStatus.COMPLETED, result.error
    assert provider.saw_active_failure
    assert provider.saw_recovery
    assert provider.saw_verified_anchor
    assert "return dividend / divisor" in (repository / "calculator.py").read_text(encoding="utf-8")
    events = trace.read()
    blocked = [event for event in events if event.type == "episode.repeat_blocked"]
    assert len(blocked) == 1
    assert blocked[0].data["call_id"] == "repeated-write"
    failures = store.memory.query(
        MemoryQuery(
            task_id=task.id,
            text="calculator patch failure",
            kinds=[MemoryKind.EPISODIC],
            paths=["calculator.py"],
            error_kinds=[ErrorKind.EXECUTION_ERROR],
            episode_outcomes=["failed"],
        )
    )
    recoveries = store.memory.query(
        MemoryQuery(
            task_id=task.id,
            text="corrected calculator patch",
            kinds=[MemoryKind.EPISODIC],
            paths=["calculator.py"],
            episode_outcomes=["recovered"],
        )
    )
    assert len(failures.hits) == 1
    assert len(recoveries.hits) == 1
    recovered_reference = recoveries.hits[0].record.content["reference"]
    assert isinstance(recovered_reference, dict)
    assert recovered_reference["recovers_episode_ids"] == [failures.hits[0].record.id]
    assert result.report is not None
    assert result.report.episode_recoveries == 1
    assert result.report.last_verified_episode_id is not None
    metrics = TaskMetrics.from_events(task.id, events)
    assert metrics.episode_recoveries == 1
    assert metrics.repeated_failed_actions_blocked == 1


def test_resume_uses_last_verified_episode_without_repeating_write(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    trace = EventLogger(tmp_path / "trace.jsonl")
    store = SQLiteStore(tmp_path / "state.db")
    provider = InterruptAfterResponses(
        [
            ModelResponse(tool_calls=[_plan_call("resume-plan", "Repair and verify")]),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="resume-write",
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
                        id="resume-tests",
                        name="run_tests",
                        arguments={
                            "command": ["python", "-m", "pytest", "-q", "test_calculator.py"]
                        },
                    )
                ]
            ),
        ]
    )
    task = Task(
        id="episode-checkpoint-resume",
        goal="Repair calculator and resume only after verified work",
        repository=str(repository),
    )
    first_runtime = AgentRuntime(provider, _gateway(repository, trace), trace, store)

    with pytest.raises(KeyboardInterrupt, match="verified checkpoint"):
        first_runtime.run(task)

    persisted = store.get_task(task.id)
    checkpoint = store.get_checkpoint(task.id)
    assert checkpoint.episodic_memory is not None
    verified_id = checkpoint.episodic_memory.last_verified_episode_id
    assert verified_id is not None
    records_before = store.memory.list_records(task.id)
    resume_provider = ResumeAnchorProvider(verified_id)
    legacy_checkpoint = checkpoint.model_copy(update={"episodic_memory": None})
    result = AgentRuntime(
        resume_provider,
        _gateway(repository, trace),
        trace,
        store,
    ).resume(persisted, legacy_checkpoint)

    assert result.status is TaskStatus.COMPLETED, result.error
    assert resume_provider.saw_anchor
    assert result.report is not None
    assert result.report.last_verified_episode_id == verified_id
    assert len(store.memory.list_records(task.id)) == len(records_before)
    tool_results = store.list_tool_results(task.id)
    assert [item.call_id for item in tool_results].count("resume-write") == 1
