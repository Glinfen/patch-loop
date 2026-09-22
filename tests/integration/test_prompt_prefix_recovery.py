"""PPS-06 exercises the actual Runtime, Provider journal and Effect recovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from patchloop.context import ContextEngine
from patchloop.domain import (
    AppendOnlyOptimizationVersion,
    PromptCacheLayout,
    Task,
    TaskBudget,
    TaskExecutionConfig,
    TaskRuntimeCondition,
    TaskStatus,
    ToolCall,
)
from patchloop.persistence import CheckpointSchemaError, RuntimeCheckpoint, SQLiteStore
from patchloop.persistence_contracts import LeaseLost, ProviderRequestStatus
from patchloop.prompt_cache import SUMMARY_PREFIX, PromptCacheCoordinator
from patchloop.prompt_cache.coordinator import compute_prefix_budget
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.providers.base import ModelUsage, ProviderRequestPurpose
from patchloop.providers.contracts import (
    ChatDialect,
    ProviderBinding,
    ProviderContinuation,
    ProviderError,
    ProviderErrorKind,
)
from patchloop.runtime import AgentRuntime
from patchloop.session import SessionService
from patchloop.tools import ToolContext, ToolGateway
from patchloop.tools.base import Tool, ToolInputModel


class _Input(ToolInputModel):
    value: int


class _Observe(Tool):
    name = "observe"
    description = "Read a deterministic observation."
    input_model = _Input

    def __init__(self, calls: list[int], *, long: bool = False):
        self.calls = calls
        self.long = long

    def run(self, arguments, context):
        value = _Input.model_validate(arguments).value
        self.calls.append(value)
        return f"observation {value}: " + ("evidence " * 250 if self.long else "ok")


def _response(*values: int) -> ModelResponse:
    return ModelResponse(
        content="Inspect observations.",
        tool_calls=[
            ToolCall(id=f"call-{v}", name="observe", arguments={"value": v}) for v in values
        ],
        usage=ModelUsage(
            input_tokens=100, output_tokens=10, cache_hit_tokens=60, cache_miss_tokens=40
        ),
    )


class _CrashStore(SQLiteStore):
    crash_at: str | None = None

    def begin_provider_request(self, request, **kwargs):
        if self.crash_at == "before_request":
            self.crash_at = None
            raise KeyboardInterrupt("before request creation")
        return super().begin_provider_request(request, **kwargs)

    def prepare_effect_batch(self, *args, **kwargs):
        if self.crash_at == "response_ready":
            self.crash_at = None
            raise KeyboardInterrupt("after response persistence")
        return super().prepare_effect_batch(*args, **kwargs)

    def save_checkpoint(self, checkpoint, **kwargs):
        super().save_checkpoint(checkpoint, **kwargs)
        if self.crash_at == "usage" and checkpoint.accounted_provider_request_ids:
            self.crash_at = None
            raise KeyboardInterrupt("after usage checkpoint")


class _CrashToolRuntime(AgentRuntime):
    def _execute_or_replay(self, task, call, **kwargs):
        result = super()._execute_or_replay(task, call, **kwargs)
        if call.id == "call-0":
            raise KeyboardInterrupt("after first tool committed")
        return result


def test_restore_rejects_task_and_checkpoint_optimization_version_conflict(
    tmp_path: Path,
) -> None:
    task = Task(
        id="optimization-conflict",
        repository=str(tmp_path),
        goal="Inspect the repository.",
        execution=TaskExecutionConfig(
            prompt_cache_layout=PromptCacheLayout.APPEND_ONLY,
            append_only_optimization="balanced_v1",
        ),
    )
    messages = PromptCacheCoordinator.initial_messages(
        "system",
        task.goal,
        layout=PromptCacheLayout.APPEND_ONLY,
    )
    coordinator = PromptCacheCoordinator.bootstrap(
        messages,
        [],
        layout=PromptCacheLayout.APPEND_ONLY,
        optimization_version="balanced_v1",
    )
    checkpoint = RuntimeCheckpoint(
        task_id=task.id,
        next_step_index=0,
        messages=messages,
        tool_specifications=[],
        **coordinator.checkpoint_fields(),
    )
    runtime = AgentRuntime(
        FakeProvider([ModelResponse(content="done")]),
        ToolGateway(ToolContext(tmp_path), []),
    )

    restored = runtime._restore_prompt_cache(task, checkpoint)
    assert restored.append_only_state.optimization_version == "balanced_v1"
    conflicting = task.model_copy(
        update={
            "execution": TaskExecutionConfig(
                prompt_cache_layout=PromptCacheLayout.APPEND_ONLY,
                append_only_optimization="baseline_v1",
            )
        }
    )
    with pytest.raises(CheckpointSchemaError, match="optimization version conflicts"):
        runtime._restore_prompt_cache(conflicting, checkpoint)
    mismatched_generation = checkpoint.model_copy(
        update={
            "append_only_state": checkpoint.append_only_state.model_copy(
                update={"epoch_generation": 1}
            )
        }
    )
    with pytest.raises(CheckpointSchemaError, match="cannot be safely restored"):
        runtime._restore_prompt_cache(task, mismatched_generation)


def test_old_checkpoint_fields_restore_with_v2_publication_and_exact_request(
    tmp_path: Path,
) -> None:
    class CrashBeforeSecondRequestStore(SQLiteStore):
        request_count = 0

        def begin_provider_request(self, request, **kwargs):
            self.request_count += 1
            if self.request_count == 2:
                raise KeyboardInterrupt("projection saved before second request")
            return super().begin_provider_request(request, **kwargs)

    repository = tmp_path / "repo"
    repository.mkdir()
    store = CrashBeforeSecondRequestStore(tmp_path / "state.db")
    calls: list[int] = []
    task = Task(
        id="old-v2-checkpoint",
        repository=str(repository),
        goal="Read one observation.",
        execution=TaskExecutionConfig(prompt_cache_layout=PromptCacheLayout.APPEND_ONLY),
        budget=TaskBudget(max_steps=4, max_context_tokens=20_000),
    )
    with pytest.raises(KeyboardInterrupt, match="projection saved"):
        AgentRuntime(
            FakeProvider([_response(0)]),
            ToolGateway(ToolContext(repository), [_Observe(calls)]),
            state_store=store,
        ).run(task)

    saved = store.get_checkpoint(task.id)
    assert saved.memory_publication_state.schema_version == "2.0"
    assert saved.memory_publication_state.messages
    payload = saved.model_dump(mode="json")
    old_state = payload["append_only_state"]
    for field in (
        "optimization_version",
        "last_compression_attempt_step",
        "compression_instruction",
        "compression_summary_target_tokens",
    ):
        old_state.pop(field, None)
    legacy = RuntimeCheckpoint.model_validate(payload)
    assert legacy.append_only_state.optimization_version == "baseline_v1"

    provider = FakeProvider([ModelResponse(content="done")])
    result = AgentRuntime(
        provider,
        ToolGateway(ToolContext(repository), [_Observe(calls)]),
        state_store=store,
    ).resume(store.get_task(task.id), legacy)

    assert result.status is TaskStatus.COMPLETED, result.error
    assert provider.requests[0][0] == saved.messages
    assert calls == [0]


@pytest.mark.parametrize("optimization_version", ["baseline_v1", "balanced_v1"])
@pytest.mark.parametrize("boundary", ["before_request", "response_ready", "usage", "tool"])
def test_recovery_preserves_request_publication_tools_and_usage(
    tmp_path: Path,
    boundary: str,
    optimization_version: AppendOnlyOptimizationVersion,
):
    repository = tmp_path / "repo"
    repository.mkdir()
    store = _CrashStore(tmp_path / "state.db")
    store.crash_at = boundary
    calls: list[int] = []
    task = Task(
        id="prefix-task",
        repository=str(repository),
        goal="Read two observations.",
        execution=TaskExecutionConfig(
            prompt_cache_layout=PromptCacheLayout.APPEND_ONLY,
            append_only_optimization=optimization_version,
        ),
    )
    first = FakeProvider([_response(0, 1)])
    runtime_type = _CrashToolRuntime if boundary == "tool" else AgentRuntime
    runtime = runtime_type(
        first,
        ToolGateway(ToolContext(repository), [_Observe(calls)]),
        state_store=store,
    )
    with pytest.raises(KeyboardInterrupt):
        runtime.run(task)
    saved = store.get_checkpoint(task.id)
    original_request = saved.messages
    original_publication = saved.memory_publication_state
    assert saved.append_only_state.optimization_version == optimization_version
    assert saved.append_only_state.last_submitted_message_count == len(original_request)
    responses = ([_response(0, 1)] if boundary == "before_request" else []) + [
        ModelResponse(content="done", usage=ModelUsage(input_tokens=50, output_tokens=5)),
    ]
    resumed_provider = FakeProvider(responses)

    class ReplayRuntime(AgentRuntime):
        retrievals = 0

        def _retrieve_memory(self, *args, **kwargs):
            self.retrievals += 1
            return super()._retrieve_memory(*args, **kwargs)

    resumed_runtime = ReplayRuntime(
        resumed_provider,
        ToolGateway(ToolContext(repository), [_Observe(calls)]),
        state_store=store,
    )
    result = resumed_runtime.resume(store.get_task(task.id), saved)
    assert result.status is TaskStatus.COMPLETED, result.error
    assert resumed_runtime.retrievals == 1  # Only the new, final request retrieves memory.
    assert calls == [0, 1]
    final_messages = resumed_provider.requests[-1][0]
    assert final_messages[: len(original_request)] == original_request
    assert [m.tool_call_id for m in final_messages if m.role == "tool"] == ["call-0", "call-1"]
    if boundary == "before_request":
        assert resumed_provider.requests[0][0] == original_request
    assert (
        original_publication.messages
        == final_messages[
            len(original_request) - len(original_publication.messages) : len(original_request)
        ]
    )
    checkpoint = store.get_checkpoint(task.id)
    assert checkpoint.input_tokens == 150
    assert checkpoint.output_tokens == 15
    assert checkpoint.cache_hit_tokens == 60
    assert checkpoint.cache_miss_tokens == 40
    assert len(checkpoint.accounted_provider_request_ids) == 2
    if boundary == "before_request":
        assert checkpoint.unknown_model_usage_steps == []


def test_saved_response_continuation_is_preserved_in_following_request(tmp_path: Path):
    store = _CrashStore(tmp_path / "state.db")
    store.crash_at = "response_ready"
    calls: list[int] = []
    continuation = ProviderContinuation(deepseek_reasoning_content="Preserve exact reasoning.\n")
    response = _response(0).model_copy(update={"continuation": continuation})
    first = FakeProvider([response])
    gateway = ToolGateway(ToolContext(tmp_path), [_Observe(calls)])
    payload = AgentRuntime(first, gateway).provider_binding.model_dump(exclude={"fingerprint"})
    payload["dialect"] = ChatDialect.DEEPSEEK
    binding = ProviderBinding.model_validate(payload)
    first.binding = binding
    task = Task(
        repository=str(tmp_path),
        goal="Read the observation.",
        execution=TaskExecutionConfig(prompt_cache_layout=PromptCacheLayout.APPEND_ONLY),
    )
    with pytest.raises(KeyboardInterrupt):
        AgentRuntime(first, gateway, state_store=store).run(task)
    resumed = FakeProvider([ModelResponse(content="done")])
    resumed.binding = binding
    result = AgentRuntime(
        resumed,
        ToolGateway(ToolContext(tmp_path), [_Observe(calls)]),
        state_store=store,
    ).resume(store.get_task(task.id), store.get_checkpoint(task.id))
    assert result.status is TaskStatus.COMPLETED, result.error
    assistant = next(m for m in resumed.requests[0][0] if m.role == "assistant")
    assert assistant.continuation == continuation
    assert assistant.tool_calls == response.tool_calls


def test_unknown_attempt_reuses_saved_input_without_claiming_known_usage(tmp_path: Path):
    store = SQLiteStore(tmp_path / "state.db")
    task = Task(
        repository=str(tmp_path),
        goal="Answer the request.",
        execution=TaskExecutionConfig(prompt_cache_layout=PromptCacheLayout.APPEND_ONLY),
    )

    class TimeoutProvider(FakeProvider):
        def complete(self, messages, tools):
            self.requests.append((list(messages), list(tools)))
            raise ProviderError(
                ProviderErrorKind.TIMEOUT,
                "response unknown",
                request_sent=True,
                usage_unknown=True,
            )

    provider = TimeoutProvider([])
    runtime = AgentRuntime(provider, ToolGateway(ToolContext(tmp_path), []), state_store=store)
    runtime.run(task)
    checkpoint = store.get_checkpoint(task.id)
    retry = FakeProvider([ModelResponse(content="done")])
    result = AgentRuntime(
        retry,
        ToolGateway(ToolContext(tmp_path), []),
        state_store=store,
    ).resume(store.get_task(task.id), checkpoint)
    assert result.status is TaskStatus.COMPLETED
    assert retry.requests[0] == provider.requests[0]
    final = store.get_checkpoint(task.id)
    assert final.unknown_model_usage_steps == [0]
    assert final.unknown_usage_attempts >= 1
    assert final.cost_status == "unknown"


_SUMMARY = json.dumps(
    {
        "constraints": ["Read observations"],
        "paths": [],
        "decisions": [],
        "failures": [],
        "tests": [],
        "unfinished": ["Continue observations"],
        "next_step": "Read next observation",
    }
)


class _CompressingProvider(FakeProvider):
    def __init__(self, *, index=0, count=10, on_compression=None):
        super().__init__([])
        self.index = index
        self.count = count
        self.on_compression = on_compression

    def complete(self, messages, tools):
        self.requests.append((list(messages), list(tools)))
        if "PATCHLOOP_EPOCH_COMPRESSION_V1" in messages[-1].content:
            if self.on_compression is not None:
                self.on_compression()
            return ModelResponse(
                content=_SUMMARY, usage=ModelUsage(input_tokens=200, output_tokens=20)
            )
        if self.index >= self.count:
            return ModelResponse(content="done", usage=ModelUsage(input_tokens=50, output_tokens=5))
        response = _response(self.index)
        self.index += 1
        return response


class _CompressionCrashRuntime(AgentRuntime):
    fault = "crash"

    def _commit_provider_response(self, request_id, response):
        response = super()._commit_provider_response(request_id, response)
        if self.state_store.get_provider_request(request_id).purpose is (
            ProviderRequestPurpose.EPOCH_COMPRESSION
        ):
            if self.fault == "lease":
                raise LeaseLost("compression lease lost")
            raise KeyboardInterrupt("compression saved before epoch commit")
        return response


class _CompressionBoundaryStore(SQLiteStore):
    def __init__(self, path: Path, *, fault: str):
        super().__init__(path)
        self.fault = fault
        self.crashed = False

    def begin_provider_request(self, request, **kwargs):
        saved = super().begin_provider_request(request, **kwargs)
        if (
            not self.crashed
            and self.fault == "request_registered"
            and request.purpose is ProviderRequestPurpose.EPOCH_COMPRESSION
        ):
            self.crashed = True
            raise KeyboardInterrupt("request_registered")
        return saved

    def save_checkpoint(self, checkpoint, **kwargs):
        super().save_checkpoint(checkpoint, **kwargs)
        if (
            not self.crashed
            and self.fault == "epoch_saved"
            and checkpoint.cache_epoch_state is not None
            and checkpoint.cache_epoch_state.generation > 0
            and checkpoint.append_only_state.compression_request_id is None
        ):
            self.crashed = True
            raise KeyboardInterrupt("epoch_saved")


@pytest.mark.parametrize("optimization_version", ["baseline_v1", "balanced_v1"])
@pytest.mark.parametrize("fault", ["request_registered", "epoch_saved"])
def test_compression_request_and_epoch_checkpoint_recover_exactly_once(
    tmp_path: Path,
    fault: str,
    optimization_version: AppendOnlyOptimizationVersion,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    store = _CompressionBoundaryStore(tmp_path / "state.db", fault=fault)
    task = Task(
        id=f"compression-{fault.replace('_', '-')}-{optimization_version.replace('_', '-')}",
        repository=str(repository),
        goal="Read observations.",
        execution=TaskExecutionConfig(
            prompt_cache_layout=PromptCacheLayout.APPEND_ONLY,
            append_only_optimization=optimization_version,
        ),
        budget=TaskBudget(max_steps=16, max_context_tokens=4_500, max_tool_output_chars=3_000),
    )
    calls: list[int] = []
    provider = _CompressingProvider(count=6)

    with pytest.raises(KeyboardInterrupt, match=fault):
        AgentRuntime(
            provider,
            ToolGateway(ToolContext(repository), [_Observe(calls, long=True)]),
            state_store=store,
        ).run(task)

    saved = store.get_checkpoint(task.id)
    prefix = saved.append_only_state
    assert prefix.optimization_version == optimization_version
    assert prefix.last_compression_attempt_step is not None
    if fault == "request_registered":
        assert prefix.compression_request_id is not None
        pending = store.get_provider_request(prefix.compression_request_id)
        assert pending.status is ProviderRequestStatus.PENDING
        assert store.list_provider_attempts(prefix.compression_request_id) == []
    else:
        assert prefix.compression_request_id is None
        assert saved.cache_epoch_state.generation >= 1
        assert (
            sum(
                message.content.startswith(SUMMARY_PREFIX)
                for message in saved.cache_epoch_state.prefix_messages
            )
            == 1
        )

    resumed_provider = _CompressingProvider(index=provider.index, count=6)
    resumed_runtime = AgentRuntime(
        resumed_provider,
        ToolGateway(ToolContext(repository), [_Observe(calls, long=True)]),
        state_store=store,
    )
    result = resumed_runtime.resume(store.get_task(task.id), saved)

    assert result.status is TaskStatus.COMPLETED, result.error
    assert calls == list(range(6))
    first_resumed = resumed_provider.requests[0][0]
    if fault == "request_registered":
        assert first_resumed[-1].content == prefix.compression_instruction
        assert first_resumed[:-1] == saved.messages[: prefix.compression_source_message_count]
    else:
        assert "PATCHLOOP_EPOCH_COMPRESSION_V1" not in first_resumed[-1].content
    checkpoint = store.get_checkpoint(task.id)
    assert checkpoint.append_only_state.optimization_version == optimization_version
    assert (
        sum(
            message.content.startswith(SUMMARY_PREFIX)
            for message in checkpoint.cache_epoch_state.prefix_messages
        )
        == 1
    )
    all_requests = [*provider.requests, *resumed_provider.requests]
    compression_count = sum(
        "PATCHLOOP_EPOCH_COMPRESSION_V1" in messages[-1].content for messages, _ in all_requests
    )
    assert checkpoint.input_tokens == 6 * 100 + 50 + compression_count * 200
    assert len(checkpoint.accounted_provider_request_ids) == 7 + compression_count


@pytest.mark.parametrize("optimization_version", ["baseline_v1", "balanced_v1"])
def test_cancelled_compression_keeps_unknown_attempt_and_old_epoch(
    tmp_path: Path,
    optimization_version: AppendOnlyOptimizationVersion,
) -> None:
    class CancelCompressionProvider(_CompressingProvider):
        def complete(self, messages, tools):
            if "PATCHLOOP_EPOCH_COMPRESSION_V1" in messages[-1].content:
                self.requests.append((list(messages), list(tools)))
                raise ProviderError(
                    ProviderErrorKind.CANCELLED,
                    "compression cancelled",
                    request_sent=True,
                    usage_unknown=True,
                )
            return super().complete(messages, tools)

    repository = tmp_path / "repo"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    task = Task(
        id=f"cancel-compression-{optimization_version.replace('_', '-')}",
        repository=str(repository),
        goal="Read observations.",
        execution=TaskExecutionConfig(
            prompt_cache_layout=PromptCacheLayout.APPEND_ONLY,
            append_only_optimization=optimization_version,
        ),
        budget=TaskBudget(max_steps=12, max_context_tokens=4_500, max_tool_output_chars=3_000),
    )
    calls: list[int] = []
    result = AgentRuntime(
        CancelCompressionProvider(count=6),
        ToolGateway(ToolContext(repository), [_Observe(calls, long=True)]),
        state_store=store,
    ).run(task)

    assert result.status is TaskStatus.FAILED
    assert result.report is not None
    assert result.report.cost_status == "unknown"
    assert result.report.unknown_usage_attempts == 1
    checkpoint = store.get_checkpoint(task.id)
    assert checkpoint.cache_epoch_state.generation == 0
    request_id = checkpoint.append_only_state.compression_request_id
    assert request_id is not None
    attempts = store.list_provider_attempts(request_id)
    assert len(attempts) == 1
    assert attempts[0].status.value == "cancelled"
    assert attempts[0].usage_status.value == "unknown"


@pytest.mark.parametrize("optimization_version", ["baseline_v1", "balanced_v1"])
def test_invalid_compression_continues_soft_then_pauses_hard_and_defers_source(
    tmp_path: Path,
    optimization_version: AppendOnlyOptimizationVersion,
):
    class InvalidSummaryProvider(_CompressingProvider):
        def complete(self, messages, tools):
            response = super().complete(messages, tools)
            if "PATCHLOOP_EPOCH_COMPRESSION_V1" in messages[-1].content:
                return response.model_copy(update={"content": "not a JSON summary"})
            return response

    calls: list[int] = []
    store = SQLiteStore(tmp_path / "state.db")
    task = Task(
        repository=str(tmp_path),
        goal="Read observations.",
        execution=TaskExecutionConfig(
            prompt_cache_layout=PromptCacheLayout.APPEND_ONLY,
            append_only_optimization=optimization_version,
        ),
        budget=TaskBudget(max_steps=20, max_context_tokens=4_500, max_tool_output_chars=3_000),
    )
    provider = InvalidSummaryProvider()
    runtime = AgentRuntime(
        provider,
        ToolGateway(ToolContext(tmp_path), [_Observe(calls, long=True)]),
        state_store=store,
    )
    result = runtime.run(task)
    assert result.runtime_condition is TaskRuntimeCondition.PAUSED, result.error
    checkpoint = store.get_checkpoint(task.id)
    assert checkpoint.append_only_state.deferred_compression_fingerprint is not None
    assert checkpoint.cache_epoch_state.generation == 0
    compressions = [
        i
        for i, (messages, _) in enumerate(provider.requests)
        if "PATCHLOOP_EPOCH_COMPRESSION_V1" in messages[-1].content
    ]
    assert compressions
    if optimization_version == "baseline_v1":
        assert len(compressions) >= 2
        # Soft failure still permits an ordinary request before the next attempt.
        assert compressions[1] > compressions[0] + 1
    else:
        assert len(compressions) == 1  # balanced_v1 backoff prevents a redundant retry.
    retry = FakeProvider([])
    resumed = AgentRuntime(
        retry,
        ToolGateway(ToolContext(tmp_path), [_Observe(calls, long=True)]),
        state_store=store,
    ).resume(store.get_task(task.id), checkpoint)
    assert resumed.runtime_condition is TaskRuntimeCondition.PAUSED
    assert retry.requests == []


@pytest.mark.parametrize("optimization_version", ["baseline_v1", "balanced_v1"])
@pytest.mark.parametrize("fault", ["crash", "lease"])
def test_compression_response_recovery_and_inputs_during_compression(
    tmp_path: Path,
    fault: str,
    optimization_version: AppendOnlyOptimizationVersion,
):
    repository = tmp_path / "repo"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    service = SessionService(store)
    session = service.create(str(repository))
    task = service.start_task(
        session.id,
        "Read observations.",
        execution=TaskExecutionConfig(
            prompt_cache_layout=PromptCacheLayout.APPEND_ONLY,
            append_only_optimization=optimization_version,
        ),
        budget=TaskBudget(max_steps=20, max_context_tokens=4_500, max_tool_output_chars=3_000),
    )
    calls: list[int] = []
    provider = _CompressingProvider(
        on_compression=lambda: service.append_message(
            session.id,
            "Keep this late constraint.",
            client_submission_id="compression-input",
        )
    )
    runtime = _CompressionCrashRuntime(
        provider,
        ToolGateway(ToolContext(repository), [_Observe(calls, long=True)]),
        state_store=store,
    )
    runtime.fault = fault
    with pytest.raises(KeyboardInterrupt if fault == "crash" else LeaseLost):
        runtime.run(task)
    saved = store.get_checkpoint(task.id)
    assert saved.append_only_state.compression_request_id is not None
    assert saved.append_only_state.optimization_version == optimization_version
    assert saved.append_only_state.last_compression_attempt_step is not None
    if optimization_version == "balanced_v1":
        assert "PATCHLOOP_EPOCH_COMPRESSION_BALANCED_V1" in (
            saved.append_only_state.compression_instruction or ""
        )
        assert saved.append_only_state.compression_summary_target_tokens is not None
    else:
        assert saved.append_only_state.compression_summary_target_tokens is None
    source_count = saved.append_only_state.last_submitted_message_count
    assert provider.requests[-1][0][:-1] == saved.messages[:source_count]
    resumed_provider = _CompressingProvider(index=provider.index)
    resumed_runtime = AgentRuntime(
        resumed_provider,
        ToolGateway(ToolContext(repository), [_Observe(calls, long=True)]),
        state_store=store,
    )
    result = resumed_runtime.resume(store.get_task(task.id), saved)
    assert result.status is TaskStatus.COMPLETED, result.error
    assert calls == list(range(10))
    assert "PATCHLOOP_EPOCH_COMPRESSION_V1" not in resumed_provider.requests[0][0][-1].content
    assert (
        sum(m.content == "Keep this late constraint." for m in resumed_provider.requests[0][0]) == 1
    )
    checkpoint = store.get_checkpoint(task.id)
    assert checkpoint.cache_epoch_state.generation >= 2
    assert checkpoint.append_only_state.optimization_version == optimization_version
    all_requests = provider.requests + resumed_provider.requests
    compressions = 0
    previous = None
    for messages, tools in all_requests:
        if "PATCHLOOP_EPOCH_COMPRESSION_V1" in messages[-1].content:
            assert messages[:-1] == previous
            compressions += 1
            previous = None
        else:
            if previous is not None:
                assert messages[: len(previous)] == previous
            previous = messages
            policy = resumed_runtime._prompt_cache.optimization_policy
            assert policy is not None
            budget = compute_prefix_budget(
                task.budget,
                resumed_runtime.provider_binding,
                tools,
                policy=policy,
            )
            assert ContextEngine.estimate_messages(messages) + ContextEngine.estimate_tools(
                tools
            ) <= (budget.ordinary_limit)
    assert checkpoint.input_tokens == 10 * 100 + 50 + compressions * 200
    assert len(checkpoint.accounted_provider_request_ids) == 11 + compressions
