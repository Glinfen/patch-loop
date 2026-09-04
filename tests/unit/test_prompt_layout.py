from patchloop.domain import PromptCacheLayout, Task, TaskExecutionConfig, ToolCall
from patchloop.events import EventLogger
from patchloop.prompt_cache import PromptLayout
from patchloop.providers import FakeProvider, ModelMessage, ModelResponse, ToolSpec
from patchloop.runtime import SYSTEM_PROMPT, AgentRuntime
from patchloop.tools import ListFilesTool, ToolContext, ToolGateway


def test_prompt_layout_has_explicit_rollback_and_stable_modes() -> None:
    legacy = PromptLayout(PromptCacheLayout.LEGACY)
    stable = PromptLayout(PromptCacheLayout.STABLE)

    assert legacy.prefix_message_count == 2
    assert stable.prefix_message_count == 3
    assert len(legacy.initial_messages("system", "goal", project_instructions="rules")) == 2
    stable_messages = stable.initial_messages("system", "goal", project_instructions="rules")
    assert len(stable_messages) == 3
    assert stable_messages[0].content == "system"
    assert stable_messages[1].content.startswith("PATCHLOOP_PROJECT_INSTRUCTIONS_V1")
    assert stable_messages[2].content == "goal"
    assert TaskExecutionConfig().prompt_cache_layout is PromptCacheLayout.LEGACY


def test_prompt_layout_freezes_tool_specification_copies() -> None:
    original = [ToolSpec(name="read", description="read", parameters={"type": "object"})]

    frozen = PromptLayout.freeze_tools(original)
    original[0].description = "changed"

    assert frozen[0].description == "read"
    assert frozen[0] is not original[0]


def test_runtime_memory_message_is_independent_from_the_system_message() -> None:
    message = PromptLayout.runtime_memory_message("runtime state")

    assert message == ModelMessage(role="system", content="runtime state")


def test_stable_runtime_keeps_project_snapshot_and_system_prefix_fixed(tmp_path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    provider = FakeProvider([ModelResponse(content="done")])
    gateway = ToolGateway(
        ToolContext(repository),
        [ListFilesTool()],
        EventLogger(tmp_path / "trace.jsonl"),
    )
    task = Task(
        goal="inspect repository",
        repository=str(repository),
        execution=TaskExecutionConfig(
            prompt_cache_layout=PromptCacheLayout.STABLE,
            project_instructions="Keep the change narrow.",
        ),
    )

    result = AgentRuntime(provider, gateway).run(task)

    assert result.result == "done"
    messages = provider.requests[0][0]
    assert [message.role for message in messages[:3]] == ["system", "system", "user"]
    assert messages[0].content == SYSTEM_PROMPT
    assert messages[1].content.startswith("PATCHLOOP_PROJECT_INSTRUCTIONS_V1")
    assert messages[2].content == "inspect repository"


def test_stable_runtime_publishes_memory_snapshot_once_then_deltas(tmp_path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=[ToolCall(id="list-call", name="list_files", arguments={})]),
            ModelResponse(content="done"),
        ]
    )
    gateway = ToolGateway(
        ToolContext(repository),
        [ListFilesTool()],
        EventLogger(tmp_path / "trace.jsonl"),
    )
    task = Task(
        goal="inspect repository",
        repository=str(repository),
        execution=TaskExecutionConfig(prompt_cache_layout=PromptCacheLayout.STABLE),
    )

    result = AgentRuntime(provider, gateway).run(task)

    assert result.result == "done"
    assert len(provider.requests) == 2
    first_messages = [message.content for message in provider.requests[0][0]]
    second_messages = [message.content for message in provider.requests[1][0]]
    assert sum("PATCHLOOP_MEMORY_SNAPSHOT_V1" in content for content in first_messages) == 1
    assert sum("PATCHLOOP_MEMORY_SNAPSHOT_V1" in content for content in second_messages) == 1
    assert sum("PATCHLOOP_MEMORY_DELTA_V1" in content for content in second_messages) == 1
