"""PGW-08 Session flow through the real Chat and Responses adapters."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from patchloop.domain import TaskStatus
from patchloop.persistence import SQLiteStore
from patchloop.providers.chat import ChatCompletionsAdapter
from patchloop.providers.contracts import (
    ChatDialect,
    ProviderAuth,
    ProviderBinding,
    ProviderCapabilities,
    ProviderGeneration,
    ProviderProtocol,
    ProviderTransportConfig,
)
from patchloop.providers.gateway import ProviderGateway
from patchloop.providers.responses import ResponsesAdapter
from patchloop.runtime import AgentRuntime
from patchloop.session import SessionService
from patchloop.tools import ToolContext, ToolGateway


class _JsonResponse:
    status_code = 200

    def __init__(self, body: dict[str, object]) -> None:
        self.headers = {"content-type": "application/json"}
        self.body = json.dumps(body).encode("utf-8")

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        yield self.body


class _JsonTransport:
    def __init__(self, body: dict[str, object]) -> None:
        self.body = body
        self.requests = 0

    @asynccontextmanager
    async def client_scope(self) -> AsyncIterator[None]:
        yield

    @asynccontextmanager
    async def open(self, encoded, timeouts) -> AsyncIterator[_JsonResponse]:
        del encoded, timeouts
        self.requests += 1
        yield _JsonResponse(self.body)


def _binding(protocol: ProviderProtocol) -> ProviderBinding:
    return ProviderBinding(
        profile_id=f"e2e-{protocol.value}",
        protocol=protocol,
        dialect=ChatDialect.STANDARD,
        model="fixture-model",
        base_url="https://provider.example/v1",
        auth=ProviderAuth.NONE,
        capabilities=ProviderCapabilities(
            tools=True,
            multiple_tool_calls=True,
            streaming=False,
            context_window_tokens=32_000,
            max_output_tokens=4_096,
            usage_supported=True,
        ),
        generation=ProviderGeneration(max_output_tokens=1_024),
        transport=ProviderTransportConfig(streaming=False, max_retries=0),
    )


@pytest.mark.parametrize(
    ("protocol", "adapter", "body"),
    [
        (
            ProviderProtocol.CHAT_COMPLETIONS,
            ChatCompletionsAdapter(),
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "chat complete"},
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        ),
        (
            ProviderProtocol.RESPONSES,
            ResponsesAdapter(),
            {
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "id": "message-1",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "responses complete"}],
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        ),
    ],
)
def test_session_uses_same_runtime_path_for_both_protocols(
    tmp_path: Path,
    protocol: ProviderProtocol,
    adapter: object,
    body: dict[str, object],
) -> None:
    repository = tmp_path / protocol.value
    repository.mkdir()
    store = SQLiteStore(tmp_path / f"{protocol.value}.sqlite")
    binding = _binding(protocol)
    transport = _JsonTransport(body)
    provider = ProviderGateway(binding, adapter, transport)  # type: ignore[arg-type]
    runtime = AgentRuntime(
        provider,
        ToolGateway(ToolContext(repository), []),
        state_store=store,
    )
    service = SessionService(store, runtime)
    protocol_id = protocol.value.replace("_", "-")
    session = service.create(str(repository), session_id=f"session-{protocol_id}")
    service.start_task(session.id, "answer", task_id=f"task-{protocol_id}")

    result = service.resume(session.id)

    assert result.status is TaskStatus.COMPLETED
    assert result.execution.provider == binding
    assert transport.requests == 1
