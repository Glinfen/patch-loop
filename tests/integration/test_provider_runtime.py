"""PGW-08 Runtime integration with durable provider request lifecycles."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from patchloop.domain import TaskRuntimeCondition, TaskStatus
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import ProviderAttemptStatus, ProviderRequestStatus
from patchloop.providers.base import (
    ModelMessage,
    ModelResponse,
    ProviderEvent,
    ProviderEventObserver,
    ProviderEventType,
    ProviderRequest,
    ToolSpec,
)
from patchloop.providers.contracts import (
    ChatDialect,
    ProviderAuth,
    ProviderBinding,
    ProviderCapabilities,
    ProviderError,
    ProviderErrorKind,
    ProviderGeneration,
    ProviderProtocol,
    ProviderTransportConfig,
    ReasoningTransport,
)
from patchloop.runtime import AgentRuntime
from patchloop.session import SessionService
from patchloop.tools import ToolContext, ToolGateway


def _binding(protocol: ProviderProtocol) -> ProviderBinding:
    return ProviderBinding(
        profile_id=f"runtime-{protocol.value}",
        protocol=protocol,
        dialect=ChatDialect.STANDARD,
        model=f"{protocol.value}-model",
        base_url="https://provider.example/v1",
        auth=ProviderAuth.NONE,
        capabilities=ProviderCapabilities(
            tools=True,
            multiple_tool_calls=True,
            streaming=True,
            reasoning_transport=(
                ReasoningTransport.RESPONSES_ITEMS
                if protocol is ProviderProtocol.RESPONSES
                else ReasoningTransport.NONE
            ),
            context_window_tokens=32_000,
            max_output_tokens=4_096,
            usage_supported=True,
        ),
        generation=ProviderGeneration(max_output_tokens=1_024),
        transport=ProviderTransportConfig(streaming=True, max_retries=0),
    )


class _ScriptedGateway:
    def __init__(
        self,
        binding: ProviderBinding,
        responses: Iterable[ModelResponse],
        *,
        failure: ProviderError | None = None,
    ) -> None:
        self.binding = binding
        self.responses = iter(responses)
        self.failure = failure
        self.requests: list[ProviderRequest] = []

    def complete_request(
        self,
        request: ProviderRequest,
        *,
        control=None,
        on_event: ProviderEventObserver | None = None,
    ) -> ModelResponse:
        del control
        self.requests.append(request)
        if on_event is not None:
            on_event(
                ProviderEvent(
                    type=ProviderEventType.REQUEST_STARTED,
                    request_id=request.request_id,
                    sequence=0,
                )
            )
            on_event(
                ProviderEvent(
                    type=ProviderEventType.ATTEMPT_STARTED,
                    request_id=request.request_id,
                    attempt_id=f"attempt-{len(self.requests)}",
                    sequence=1,
                )
            )
        if self.failure is not None:
            if on_event is not None:
                on_event(
                    ProviderEvent(
                        type=ProviderEventType.TEXT_DELTA,
                        request_id=request.request_id,
                        attempt_id=f"attempt-{len(self.requests)}",
                        sequence=2,
                        delta="incomplete",
                    )
                )
            raise self.failure
        response = next(self.responses).model_copy(update={"request_id": request.request_id})
        if on_event is not None:
            on_event(
                ProviderEvent(
                    type=ProviderEventType.RESPONSE_COMPLETED,
                    request_id=request.request_id,
                    attempt_id=f"attempt-{len(self.requests)}",
                    sequence=2,
                    response=response,
                )
            )
        return response

    def complete(self, messages: list[ModelMessage], tools: list[ToolSpec]) -> ModelResponse:
        del messages, tools
        raise AssertionError("Runtime must use complete_request")


@pytest.mark.parametrize(
    "protocol",
    [ProviderProtocol.CHAT_COMPLETIONS, ProviderProtocol.RESPONSES],
)
def test_runtime_journals_and_completes_both_protocol_families(
    tmp_path: Path,
    protocol: ProviderProtocol,
) -> None:
    repository = tmp_path / protocol.value
    repository.mkdir()
    store = SQLiteStore(tmp_path / f"{protocol.value}.sqlite")
    binding = _binding(protocol)
    provider = _ScriptedGateway(binding, [ModelResponse(content="done")])
    runtime = AgentRuntime(
        provider,
        ToolGateway(ToolContext(repository), []),
        state_store=store,
    )
    service = SessionService(store, runtime)
    protocol_id = protocol.value.replace("_", "-")
    session = service.create(str(repository), session_id=f"session-{protocol_id}")
    task = service.start_task(session.id, "answer", task_id=f"task-{protocol_id}")

    assert task.execution.provider == binding
    result = service.resume(session.id)

    assert result.status is TaskStatus.COMPLETED
    request_id = provider.requests[0].request_id
    request = store.get_provider_request(request_id)
    assert request.status is ProviderRequestStatus.COMPLETED
    assert request.binding_fingerprint == binding.fingerprint
    attempts = store.list_provider_attempts(request_id)
    assert [attempt.status for attempt in attempts] == [ProviderAttemptStatus.SUCCEEDED]
    assert store.get_checkpoint(result.id).accounted_provider_request_ids == [request_id]


class _CrashBeforeEffectBatchStore(SQLiteStore):
    crashed = False

    def prepare_effect_batch(self, step, effects, **kwargs):
        if not self.crashed:
            self.crashed = True
            raise KeyboardInterrupt("before Effect batch")
        return super().prepare_effect_batch(step, effects, **kwargs)


def test_response_ready_is_reused_without_another_provider_call(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = _CrashBeforeEffectBatchStore(tmp_path / "state.sqlite")
    binding = _binding(ProviderProtocol.CHAT_COMPLETIONS)
    first = _ScriptedGateway(binding, [ModelResponse(content="persisted")])
    runtime = AgentRuntime(
        first,
        ToolGateway(ToolContext(repository), []),
        state_store=store,
    )
    service = SessionService(store, runtime)
    session = service.create(str(repository), session_id="session-recovery")
    task = service.start_task(session.id, "answer", task_id="task-recovery")

    with pytest.raises(KeyboardInterrupt, match="before Effect batch"):
        service.resume(session.id)

    request_id = first.requests[0].request_id
    assert store.get_provider_request(request_id).status is ProviderRequestStatus.RESPONSE_READY
    replay = _ScriptedGateway(binding, [])
    resumed = AgentRuntime(
        replay,
        ToolGateway(ToolContext(repository), []),
        state_store=store,
    ).resume(store.get_task(task.id), store.get_checkpoint(task.id))

    assert resumed.status is TaskStatus.COMPLETED
    assert resumed.result == "persisted"
    assert replay.requests == []
    assert store.get_provider_request(request_id).status is ProviderRequestStatus.COMPLETED


def test_partial_provider_output_pauses_without_persisting_effects(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.sqlite")
    binding = _binding(ProviderProtocol.RESPONSES)
    provider = _ScriptedGateway(
        binding,
        [],
        failure=ProviderError(
            ProviderErrorKind.TRUNCATED,
            "provider stream was incomplete",
            request_sent=True,
            partial_output=True,
            usage_unknown=True,
        ),
    )
    runtime = AgentRuntime(
        provider,
        ToolGateway(ToolContext(repository), []),
        state_store=store,
    )
    service = SessionService(store, runtime)
    session = service.create(str(repository), session_id="session-partial")
    task = service.start_task(session.id, "answer", task_id="task-partial")

    result = service.resume(session.id)

    assert result.status is TaskStatus.RUNNING
    assert result.runtime_condition is TaskRuntimeCondition.PAUSED
    assert store.list_effects(task.id) == []
    assert store.list_steps(task.id)[0].model_response is None
    request_id = provider.requests[0].request_id
    assert store.get_provider_request(request_id).status is ProviderRequestStatus.PENDING
    assert store.list_provider_attempts(request_id)[0].status is ProviderAttemptStatus.FAILED
