import json
from pathlib import Path

from patchloop.context import ContextEngine
from patchloop.domain import Task, TaskBudget, TaskStatus, ToolCall
from patchloop.events import EventLogger
from patchloop.providers import ModelMessage, ModelResponse, ToolSpec
from patchloop.runtime import AgentRuntime
from patchloop.tools import ReadFileTool, ToolContext, ToolGateway


class MemoryAwareProvider:
    def __init__(self, paths: list[str], context_budget: int) -> None:
        self.paths = paths
        self.context_budget = context_budget
        self.request_count = 0
        self.saw_memory = False

    @property
    def name(self) -> str:
        return "memory-aware"

    def complete(
        self,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        estimated = ContextEngine.estimate_messages(messages) + ContextEngine.estimate_tools(tools)
        if estimated > self.context_budget:
            raise AssertionError(f"context exceeded budget: {estimated}")
        if self.request_count < len(self.paths):
            path = self.paths[self.request_count]
            self.request_count += 1
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id=f"read-{self.request_count}",
                        name="read_file",
                        arguments={"path": path, "max_chars": 50_000},
                    )
                ]
            )
        joined = "\n".join(message.content for message in messages)
        self.saw_memory = "PATCHLOOP_TASK_MEMORY_V1" in joined
        if "EARLY-CONCLUSION=blue-widget" not in joined:
            raise AssertionError("early conclusion was lost during context compaction")
        self.request_count += 1
        return ModelResponse(content="Completed using EARLY-CONCLUSION=blue-widget from memory.")


def test_long_task_stays_in_budget_and_keeps_early_evidence(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "early.py").write_text(
        "EARLY-CONCLUSION=blue-widget\n" + "early_noise = 1\n" * 2_000,
        encoding="utf-8",
    )
    noise_paths: list[str] = []
    for index in range(5):
        path = f"noise_{index}.py"
        noise_paths.append(path)
        (repository / path).write_text(
            f"NOISE_{index} = '" + "x" * 12_000 + "'\n",
            encoding="utf-8",
        )
    context_budget = 900
    provider = MemoryAwareProvider(["early.py", *noise_paths], context_budget)
    trace = EventLogger(tmp_path / "trace.jsonl")
    gateway = ToolGateway(ToolContext(repository), [ReadFileTool()], trace)
    task = Task(
        goal="Find EARLY-CONCLUSION and retain it until the long inspection is complete",
        repository=str(repository),
        budget=TaskBudget(
            max_steps=10,
            max_context_tokens=context_budget,
            max_tool_output_chars=400,
            context_recent_steps=1,
            max_repeated_actions=10,
        ),
    )

    result = AgentRuntime(provider, gateway, trace).run(task)

    assert result.status is TaskStatus.COMPLETED, result.error
    assert provider.saw_memory
    assert result.report is not None
    assert result.report.context_windows == 7
    assert result.report.context_compactions > 0
    assert result.report.max_context_tokens_used <= context_budget
    assert result.report.truncated_tool_outputs == 6
    assert len(gateway.history[0].output) > task.budget.max_tool_output_chars
    context_events = [event for event in trace.read() if event.type == "context.built"]
    assert len(context_events) == 7
    assert all(
        event.data["debug"]["estimated_tokens"] <= context_budget for event in context_events
    )
    assert any(event.data["memory"] is not None for event in context_events)
    assert any(
        "EARLY-CONCLUSION=blue-widget" in json.dumps(event.data["memory"], ensure_ascii=False)
        for event in context_events
        if event.data["memory"] is not None
    )
    expected = json.loads(
        (Path(__file__).parents[2] / "benchmarks" / "results" / "week06_context.json").read_text(
            encoding="utf-8"
        )
    )
    assert expected == {
        "scenario": "long-task-early-evidence",
        "context_budget_tokens": context_budget,
        "max_tool_output_chars": task.budget.max_tool_output_chars,
        "recent_steps": task.budget.context_recent_steps,
        "provider_calls": result.report.context_windows,
        "tool_calls": result.report.tool_calls,
        "context_compactions": result.report.context_compactions,
        "max_context_tokens_used": result.report.max_context_tokens_used,
        "truncated_tool_outputs": result.report.truncated_tool_outputs,
        "early_evidence_retained": provider.saw_memory,
        "completed": result.status is TaskStatus.COMPLETED,
    }
