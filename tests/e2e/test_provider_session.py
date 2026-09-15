"""PGW-08 Session flow through the real Chat and Responses adapters."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import SecretStr

from patchloop.domain import TaskStatus
from patchloop.events import EventLogger
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import ProviderAttemptStatus, ProviderRequestStatus
from patchloop.providers.chat import ChatCompletionsAdapter
from patchloop.providers.contracts import (
    ChatDialect,
    ProviderAuth,
    ProviderBinding,
    ProviderCapabilities,
    ProviderGeneration,
    ProviderPricing,
    ProviderProtocol,
    ProviderTransportConfig,
    ReasoningTransport,
)
from patchloop.providers.gateway import ProviderGateway
from patchloop.providers.responses import ResponsesAdapter
from patchloop.providers.transport import HttpxTransport
from patchloop.runtime import AgentRuntime
from patchloop.session import SessionService
from patchloop.tools import ListFilesTool, ReadFileTool, ToolContext, ToolGateway, WriteFileTool


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


def _binding(protocol: ProviderProtocol, *, base_url: str | None = None) -> ProviderBinding:
    return ProviderBinding(
        profile_id=f"e2e-{protocol.value}",
        protocol=protocol,
        dialect=ChatDialect.STANDARD,
        model="fixture-model",
        base_url=base_url or "https://provider.example/v1",
        auth=ProviderAuth.NONE,
        capabilities=ProviderCapabilities(
            tools=True,
            multiple_tool_calls=True,
            streaming=False,
            context_window_tokens=32_000,
            max_output_tokens=4_096,
            usage_supported=True,
            reasoning_transport=(
                ReasoningTransport.RESPONSES_ITEMS
                if protocol is ProviderProtocol.RESPONSES
                else ReasoningTransport.NONE
            ),
        ),
        generation=ProviderGeneration(max_output_tokens=1_024),
        transport=ProviderTransportConfig(streaming=False, max_retries=0),
        pricing=ProviderPricing(
            version="test-local-zero",
            input_per_million=0,
            output_per_million=0,
        ),
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


def _tool_response(protocol: ProviderProtocol) -> dict[str, object]:
    calls = [
        ("call-list", "list_files", {"path": "."}),
        ("call-bad", "read_file", {"path": 3}),
        ("call-denied", "write_file", {"path": "blocked.txt", "content": "denied"}),
    ]
    if protocol is ProviderProtocol.CHAT_COMPLETIONS:
        return {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "checking",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                            for call_id, name, arguments in calls
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4},
        }
    return {
        "object": "response",
        "status": "completed",
        "output": [
            {
                "id": f"item-{call_id}",
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
            }
            for call_id, name, arguments in calls
        ],
        "usage": {"input_tokens": 8, "output_tokens": 4},
    }


def _final_response(protocol: ProviderProtocol) -> dict[str, object]:
    if protocol is ProviderProtocol.CHAT_COMPLETIONS:
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "loopback complete"},
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 2},
        }
    return {
        "object": "response",
        "status": "completed",
        "output": [
            {
                "id": "message-final",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "loopback complete"}],
            }
        ],
        "usage": {"input_tokens": 12, "output_tokens": 2},
    }


@pytest.mark.parametrize(
    ("protocol", "adapter", "path"),
    [
        pytest.param(
            ProviderProtocol.CHAT_COMPLETIONS,
            ChatCompletionsAdapter(),
            "/chat/completions",
            id="chat",
        ),
        pytest.param(
            ProviderProtocol.RESPONSES,
            ResponsesAdapter(),
            "/responses",
            id="responses",
        ),
    ],
)
def test_loopback_http_session_core(
    tmp_path: Path,
    protocol: ProviderProtocol,
    adapter: object,
    path: str,
) -> None:
    """Both wire protocols cross real HTTP, SQLite, tools, and trace boundaries."""

    from tests.support.provider_http_server import ProviderHTTPServer

    repository = tmp_path / protocol.value
    repository.mkdir()
    (repository / "sample.txt").write_text("fixture", encoding="utf-8")
    trace = EventLogger(tmp_path / f"{protocol.value}.jsonl")
    store = SQLiteStore(tmp_path / f"{protocol.value}.sqlite")
    with ProviderHTTPServer() as server:
        server.enqueue_json(path, _tool_response(protocol))
        server.enqueue_json(path, _final_response(protocol))
        binding = _binding(protocol, base_url=server.base_url)
        tools = ToolGateway(
            ToolContext(repository),
            [ListFilesTool(), ReadFileTool(), WriteFileTool()],
            event_logger=trace,
        )
        provider = ProviderGateway(
            binding,
            adapter,  # type: ignore[arg-type]
            HttpxTransport(server.base_url, config=binding.transport),
        )
        runtime = AgentRuntime(provider, tools, event_logger=trace, state_store=store)
        service = SessionService(store, runtime)
        protocol_id = protocol.value.replace("_", "-")
        session = service.create(str(repository), session_id=f"http-{protocol_id}")
        task = service.start_task(session.id, "exercise tools", task_id=f"http-{protocol_id}")

        result = service.resume(session.id)

        assert result.status is TaskStatus.COMPLETED, result.report
        assert result.result == "loopback complete"
        assert [request["path"] for request in server.requests] == [path, path]
        assert len(tools.history) == 3
        assert tools.history[0].success is True
        assert tools.history[1].success is False
        assert tools.history[2].success is False
        assert not (repository / "blocked.txt").exists()
        effects = store.list_effects(task.id)
        assert len(effects) == 3
        request_ids = store.get_checkpoint(task.id).accounted_provider_request_ids
        requests = [store.get_provider_request(request_id) for request_id in request_ids]
        assert [request.status for request in requests] == [
            ProviderRequestStatus.COMPLETED,
            ProviderRequestStatus.COMPLETED,
        ]
        assert all(
            store.list_provider_attempts(request.request_id)[0].status
            is ProviderAttemptStatus.SUCCEEDED
            for request in requests
        )
        assert trace.read()
        second_body = json.loads(server.requests[1]["body"])
        wire_text = json.dumps(second_body)
        assert "call-list" in wire_text
        assert "call-bad" in wire_text
        assert "call-denied" in wire_text


@pytest.mark.skipif(
    os.environ.get("PATCHLOOP_ACCEPTANCE_ACTIVE") != "1"
    or not os.environ.get("PATCHLOOP_ACCEPTANCE_ENDPOINT")
    or not os.environ.get("PATCHLOOP_ACCEPTANCE_MODEL"),
    reason="PGW real-provider acceptance is not explicitly configured",
)
def test_real_provider_session_trial(tmp_path: Path) -> None:
    """Exercise a user-configured real service through a restored Session and a tool call."""

    protocol = ProviderProtocol(os.environ["PATCHLOOP_ACCEPTANCE_PROTOCOL"])
    kind = os.environ["PATCHLOOP_ACCEPTANCE_KIND"]
    endpoint = os.environ["PATCHLOOP_ACCEPTANCE_ENDPOINT"]
    model = os.environ["PATCHLOOP_ACCEPTANCE_MODEL"]
    api_key = os.environ.get("PATCHLOOP_ACCEPTANCE_API_KEY", "")
    auth = ProviderAuth.BEARER if api_key else ProviderAuth.NONE
    binding = ProviderBinding(
        profile_id=os.environ["PATCHLOOP_ACCEPTANCE_PROFILE"],
        protocol=protocol,
        dialect=(ChatDialect.DEEPSEEK if kind == "deepseek" else ChatDialect.STANDARD),
        model=model,
        base_url=endpoint,
        auth=auth,
        credential_env="PATCHLOOP_ACCEPTANCE_API_KEY" if api_key else None,
        capabilities=ProviderCapabilities(
            tools=True,
            multiple_tool_calls=True,
            streaming=False,
            reasoning_transport=(
                ReasoningTransport.RESPONSES_ITEMS
                if protocol is ProviderProtocol.RESPONSES
                else ReasoningTransport.NONE
            ),
            context_window_tokens=32_000,
            max_output_tokens=4_096,
            usage_supported=True,
        ),
        generation=ProviderGeneration(max_output_tokens=1_024, temperature=0),
        transport=ProviderTransportConfig(
            streaming=False,
            max_retries=0,
            total_timeout_seconds=180,
            idle_timeout_seconds=180,
        ),
        pricing=ProviderPricing(
            version=os.environ["PATCHLOOP_ACCEPTANCE_PRICING_VERSION"],
            input_per_million=float(os.environ["PATCHLOOP_ACCEPTANCE_INPUT_PRICE"]),
            output_per_million=float(os.environ["PATCHLOOP_ACCEPTANCE_OUTPUT_PRICE"]),
        ),
    )
    repository = tmp_path / "real-provider-workspace"
    repository.mkdir()
    (repository / "acceptance.txt").write_text("PGW-11", encoding="utf-8")
    store = SQLiteStore(tmp_path / "real-provider.sqlite")

    # Persist the Task first, then rebuild Runtime/Gateway before any request. This
    # makes every runner repetition a clean workspace plus an explicit restore.
    initial_runtime = AgentRuntime(
        ProviderGateway(
            binding,
            ChatCompletionsAdapter(binding.dialect)
            if protocol is ProviderProtocol.CHAT_COMPLETIONS
            else ResponsesAdapter(),
            HttpxTransport(
                endpoint,
                credential=SecretStr(api_key) if api_key else None,
                config=binding.transport,
            ),
        ),
        ToolGateway(ToolContext(repository), [ListFilesTool()]),
        state_store=store,
    )
    service = SessionService(store, initial_runtime)
    session = service.create(str(repository), session_id="real-provider-session")
    task = service.start_task(
        session.id,
        "Call list_files exactly once, inspect its result, then answer PGW-11 acceptance complete.",
        task_id="real-provider-task",
    )
    restored_tools = ToolGateway(ToolContext(repository), [ListFilesTool()])
    restored_runtime = AgentRuntime(
        ProviderGateway(
            binding,
            ChatCompletionsAdapter(binding.dialect)
            if protocol is ProviderProtocol.CHAT_COMPLETIONS
            else ResponsesAdapter(),
            HttpxTransport(
                endpoint,
                credential=SecretStr(api_key) if api_key else None,
                config=binding.transport,
            ),
        ),
        restored_tools,
        state_store=store,
    )

    result = restored_runtime.resume(store.get_task(task.id), store.get_checkpoint(task.id))

    assert result.status is TaskStatus.COMPLETED, result.report
    assert len(restored_tools.history) == 1
    assert restored_tools.history[0].tool_name == "list_files"
    assert restored_tools.history[0].success is True
    assert store.get_checkpoint(task.id).accounted_provider_request_ids
