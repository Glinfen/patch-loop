"""Provider-neutral request lifecycle, validation, and bounded retry policy."""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError, ValidationError  # type: ignore[import-untyped]

from patchloop.providers.base import (
    EncodedRequest,
    ModelMessage,
    ModelProvider,
    ModelResponse,
    ModelUsage,
    ProviderAdapter,
    ProviderControl,
    ProviderEvent,
    ProviderEventObserver,
    ProviderEventType,
    ProviderRequest,
    ProviderRequestPurpose,
    ToolSpec,
)
from patchloop.providers.base import (
    ProviderGateway as ProviderGatewayPort,
)
from patchloop.providers.contracts import (
    ProviderBinding,
    ProviderError,
    ProviderErrorKind,
    ProviderTransportConfig,
    ReasoningTransport,
)
from patchloop.providers.sse import SSEDecoder
from patchloop.providers.transport import AsyncTransport, TransportControlError, TransportResponse

type Clock = Callable[[], float]
type RandomSource = Callable[[], float]
type AsyncSleep = Callable[[float], Awaitable[None]]
_RETRIABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
_MAX_ADDITIONAL_ATTEMPTS = 2
_MAX_RETRY_AFTER_SECONDS = 30.0
_CONTROL_POLL_SECONDS = 0.1
_DELTA_EVENT_TYPES = frozenset(
    {
        ProviderEventType.TEXT_DELTA,
        ProviderEventType.REASONING_DELTA,
        ProviderEventType.TOOL_CALL_DELTA,
        ProviderEventType.USAGE,
    }
)


class _EventPublisher:
    def __init__(self, request_id: str, observer: ProviderEventObserver | None) -> None:
        self.request_id = request_id
        self.observer = observer
        self._sequence = 0
        self._usage_attempt_ids: set[str] = set()

    def emit(
        self,
        event_type: ProviderEventType,
        *,
        attempt_id: str | None = None,
        usage: ModelUsage | None = None,
        response: ModelResponse | None = None,
        error: ProviderError | None = None,
    ) -> None:
        event = ProviderEvent(
            type=event_type,
            request_id=self.request_id,
            attempt_id=attempt_id,
            sequence=self._sequence,
            usage=usage,
            response=response,
            error_kind=error.kind.value if error is not None else None,
            safe_message=error.safe_message if error is not None else None,
        )
        self._publish(event)

    def forward_adapter_event(self, event: ProviderEvent, attempt_id: str) -> None:
        if event.type not in _DELTA_EVENT_TYPES:
            raise ProviderError(
                ProviderErrorKind.PROTOCOL,
                "provider stream contained an unexpected lifecycle event",
            )
        self._publish(
            event.model_copy(
                update={
                    "request_id": self.request_id,
                    "attempt_id": attempt_id,
                    "sequence": self._sequence,
                }
            )
        )
        if event.type is ProviderEventType.USAGE:
            self._usage_attempt_ids.add(attempt_id)

    def has_usage_event(self, attempt_id: str) -> bool:
        return attempt_id in self._usage_attempt_ids

    def _publish(self, event: ProviderEvent) -> None:
        self._sequence += 1
        if self.observer is None:
            return
        try:
            self.observer(event)
        except Exception:
            raise ProviderError(
                ProviderErrorKind.OBSERVER,
                "provider event observer failed",
            ) from None


class ProviderGateway(ProviderGatewayPort):
    """Synchronous gateway over async transport with a single retry owner."""

    def __init__(
        self,
        binding: ProviderBinding,
        adapter: ProviderAdapter,
        transport: AsyncTransport,
        clock: Clock = time.monotonic,
        random_source: RandomSource = random.random,
        sleep: AsyncSleep = asyncio.sleep,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.binding = binding
        self.adapter = adapter
        self.transport = transport
        self.clock = clock
        self.random_source = random_source
        self.sleep = sleep
        self.wall_clock = wall_clock or (lambda: datetime.now(UTC))

    def complete_request(
        self,
        request: ProviderRequest,
        *,
        control: ProviderControl | None = None,
        on_event: ProviderEventObserver | None = None,
    ) -> ModelResponse:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "ProviderGateway.complete_request cannot run inside an event loop; "
                "call it from synchronous code"
            )
        return asyncio.run(self._complete_request(request, control=control, on_event=on_event))

    def complete(self, messages: list[ModelMessage], tools: list[ToolSpec]) -> ModelResponse:
        request = ProviderRequest(
            request_id=f"compat-{uuid4().hex}",
            task_id="compat",
            step_index=0,
            purpose=ProviderRequestPurpose.AGENT_STEP,
            messages=tuple(messages),
            tools=tuple(tools),
        )
        return self.complete_request(request)

    async def _complete_request(
        self,
        request: ProviderRequest,
        *,
        control: ProviderControl | None,
        on_event: ProviderEventObserver | None,
    ) -> ModelResponse:
        publisher = _EventPublisher(request.request_id, on_event)
        deadline = self.clock() + self.binding.transport.total_timeout_seconds
        try:
            publisher.emit(ProviderEventType.REQUEST_STARTED)
            self._validate_request(request)
            async with self.transport.client_scope():
                additional_attempts = min(
                    self.binding.transport.max_retries,
                    _MAX_ADDITIONAL_ATTEMPTS,
                )
                for attempt_number in range(additional_attempts + 1):
                    self._check_control(control)
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise self._deadline_error()

                    attempt_id = _attempt_id()
                    publisher.emit(ProviderEventType.ATTEMPT_STARTED, attempt_id=attempt_id)
                    attempt_config = self.binding.transport.model_copy(
                        update={
                            "total_timeout_seconds": min(
                                self.binding.transport.total_timeout_seconds,
                                remaining,
                            )
                        }
                    )
                    try:
                        response = await self._await_controlled(
                            self._perform_attempt(
                                request,
                                attempt_id,
                                attempt_config,
                                publisher,
                            ),
                            control=control,
                            deadline=deadline,
                        )
                    except ProviderError as error:
                        if attempt_number < additional_attempts and self._should_retry(error):
                            delay = self._retry_delay(attempt_number, error.retry_after)
                            await self._wait_backoff(delay, control=control, deadline=deadline)
                            continue
                        raise
                    return response
                raise ProviderError(
                    ProviderErrorKind.CONNECTION,
                    "provider request exhausted its attempts",
                )
        except TransportControlError:
            publisher.emit(ProviderEventType.REQUEST_CANCELLED)
            raise
        except ProviderError as error:
            if error.kind is ProviderErrorKind.CANCELLED:
                publisher.emit(ProviderEventType.REQUEST_CANCELLED)
            elif error.kind is not ProviderErrorKind.OBSERVER:
                publisher.emit(ProviderEventType.REQUEST_FAILED, error=error)
            raise
        except Exception:
            failure = ProviderError(
                ProviderErrorKind.CONNECTION,
                "provider request could not be completed",
                request_sent=True,
                usage_unknown=True,
            )
            publisher.emit(ProviderEventType.REQUEST_FAILED, error=failure)
            raise failure from None

    def _validate_request(self, request: ProviderRequest) -> None:
        capabilities = self.binding.capabilities
        if request.tools and not capabilities.tools:
            raise ProviderError(
                ProviderErrorKind.CAPABILITY,
                "the selected model does not support the requested tools",
            )
        if self.binding.transport.streaming and not capabilities.streaming:
            raise ProviderError(
                ProviderErrorKind.CAPABILITY,
                "the selected model does not support configured streaming",
            )
        if request.output_schema is not None:
            if request.tools:
                raise ProviderError(
                    ProviderErrorKind.CAPABILITY,
                    "structured output cannot be combined with tools",
                )
            if not capabilities.structured_output:
                raise ProviderError(
                    ProviderErrorKind.CAPABILITY,
                    "the selected model does not support structured output",
                )
            _check_schema(request.output_schema)

        if (
            self.binding.generation.reasoning_enabled
            and capabilities.reasoning_transport is ReasoningTransport.NONE
        ):
            raise ProviderError(
                ProviderErrorKind.CAPABILITY,
                "the selected model does not support the configured reasoning mode",
            )

        max_output_tokens = request.max_output_tokens or self.binding.generation.max_output_tokens
        max_allowed = min(
            capabilities.max_output_tokens,
            self.binding.generation.max_output_tokens,
        )
        if max_output_tokens > max_allowed:
            raise ProviderError(
                ProviderErrorKind.CAPABILITY,
                "requested output token limit exceeds the selected model limit",
            )

        self._validate_continuations(request)
        if (
            _estimated_context_tokens(request, max_output_tokens)
            > capabilities.context_window_tokens
        ):
            raise ProviderError(
                ProviderErrorKind.CAPABILITY,
                "request exceeds the selected model context limit",
            )

    def _validate_continuations(self, request: ProviderRequest) -> None:
        declared = self.binding.capabilities.reasoning_transport
        for message in request.messages:
            continuation = message.continuation
            if continuation is None:
                continue
            if not continuation.replayable:
                raise ProviderError(
                    ProviderErrorKind.CAPABILITY,
                    "request includes provider continuation that cannot be replayed safely",
                )
            if (
                continuation.deepseek_reasoning_content is not None
                and declared is not ReasoningTransport.DEEPSEEK_TEXT
            ):
                raise ProviderError(
                    ProviderErrorKind.CAPABILITY,
                    "selected model cannot replay DeepSeek reasoning continuation",
                )
            if continuation.responses_items and declared is not ReasoningTransport.RESPONSES_ITEMS:
                raise ProviderError(
                    ProviderErrorKind.CAPABILITY,
                    "selected model cannot replay Responses continuation items",
                )

    async def _perform_attempt(
        self,
        request: ProviderRequest,
        attempt_id: str,
        timeouts: ProviderTransportConfig,
        publisher: _EventPublisher,
    ) -> ModelResponse:
        try:
            encoded = self.adapter.encode(request, self.binding)
            self._validate_encoded_request(encoded)
            async with self.transport.open(encoded, timeouts) as response:
                if not 200 <= response.status_code < 300:
                    if response.status_code in _RETRIABLE_STATUS_CODES:
                        await _discard_body(response)
                    raise _http_status_error(
                        response,
                        retry_after=self._retry_after(response.headers.get("retry-after")),
                    )

                media_type = _media_type(response.headers)
                if media_type == "text/event-stream":
                    if not encoded.stream:
                        raise ProviderError(
                            ProviderErrorKind.PROTOCOL,
                            "provider streamed a response to a non-streaming request",
                            http_status=response.status_code,
                        )
                    result = await self._parse_stream(
                        response,
                        attempt_id,
                        publisher,
                        max_frame_bytes=timeouts.max_sse_frame_bytes,
                    )
                elif media_type == "application/json" or media_type.endswith("+json"):
                    body = await _read_json_object(response, timeouts.max_response_bytes)
                    result = self.adapter.parse_json(body, self.binding)
                else:
                    raise ProviderError(
                        ProviderErrorKind.PROTOCOL,
                        "provider returned an unsupported content type",
                        http_status=response.status_code,
                    )

            result = self._validate_response(result, request)
            response_with_id = result.model_copy(update={"request_id": request.request_id})
            if _usage_is_present(response_with_id.usage) and not publisher.has_usage_event(
                attempt_id
            ):
                publisher.emit(
                    ProviderEventType.USAGE,
                    attempt_id=attempt_id,
                    usage=response_with_id.usage,
                )
            publisher.emit(
                ProviderEventType.RESPONSE_COMPLETED,
                attempt_id=attempt_id,
                response=response_with_id,
            )
            return response_with_id
        except ProviderError:
            raise
        except Exception:
            raise ProviderError(
                ProviderErrorKind.PROTOCOL,
                "provider response could not be parsed",
                request_sent=True,
                usage_unknown=True,
            ) from None

    def _validate_encoded_request(self, encoded: EncodedRequest) -> None:
        if encoded.stream and (
            not self.binding.capabilities.streaming or not self.binding.transport.streaming
        ):
            raise ProviderError(
                ProviderErrorKind.CAPABILITY,
                "provider adapter enabled streaming when the selected binding does not",
            )

    async def _parse_stream(
        self,
        response: TransportResponse,
        attempt_id: str,
        publisher: _EventPublisher,
        *,
        max_frame_bytes: int,
    ) -> ModelResponse:
        try:
            reducer = self.adapter.new_reducer(self.binding)
            decoder = SSEDecoder(max_frame_bytes=max_frame_bytes)
            async for chunk in response.aiter_bytes():
                for frame in decoder.feed(chunk):
                    for event in reducer.feed(frame):
                        publisher.forward_adapter_event(event, attempt_id)
            for frame in decoder.finish():
                for event in reducer.feed(frame):
                    publisher.forward_adapter_event(event, attempt_id)
            return reducer.finish()
        except ProviderError:
            raise
        except Exception:
            raise ProviderError(
                ProviderErrorKind.PROTOCOL,
                "provider event stream was incomplete or invalid",
                request_sent=True,
                partial_output=True,
                usage_unknown=True,
            ) from None

    def _validate_response(
        self, response: ModelResponse, request: ProviderRequest
    ) -> ModelResponse:
        finish_reason = (response.finish_reason or "").lower()
        if finish_reason in {"length", "incomplete", "cancelled", "canceled"}:
            raise ProviderError(
                ProviderErrorKind.TRUNCATED,
                "provider response ended before generation completed",
                request_sent=True,
                partial_output=bool(response.content or response.tool_calls),
                usage_unknown=True,
            )
        if finish_reason in {"error", "failed"}:
            raise ProviderError(
                ProviderErrorKind.PROTOCOL,
                "provider reported a failed response",
                request_sent=True,
                usage_unknown=True,
            )
        if not response.content.strip() and not response.tool_calls:
            raise ProviderError(
                ProviderErrorKind.PROTOCOL,
                "provider returned an empty response",
                request_sent=True,
                usage_unknown=True,
            )
        if len(response.tool_calls) > 1 and not self.binding.capabilities.multiple_tool_calls:
            raise ProviderError(
                ProviderErrorKind.CAPABILITY,
                "provider returned multiple tool calls without declaring that capability",
                request_sent=True,
                usage_unknown=True,
            )

        allowed_tools = {tool.name for tool in request.tools}
        seen_ids: set[str] = set()
        for call in response.tool_calls:
            if not call.id.strip() or call.id == "missing-tool-call-id" or call.id in seen_ids:
                raise ProviderError(
                    ProviderErrorKind.PROTOCOL,
                    "provider returned a missing or duplicate tool call ID",
                    request_sent=True,
                    usage_unknown=True,
                )
            seen_ids.add(call.id)
            if call.name not in allowed_tools:
                raise ProviderError(
                    ProviderErrorKind.PROTOCOL,
                    "provider returned a tool that was not included in the request",
                    request_sent=True,
                    usage_unknown=True,
                )

        if request.output_schema is not None:
            try:
                output = json.loads(
                    response.content,
                    parse_constant=_reject_json_constant,
                )
                Draft202012Validator(request.output_schema).validate(output)
            except (json.JSONDecodeError, SchemaError, ValidationError, ValueError):
                raise ProviderError(
                    ProviderErrorKind.STRUCTURED_OUTPUT_INVALID,
                    "provider response was not valid JSON for the requested schema",
                    request_sent=True,
                    usage_unknown=True,
                ) from None
        return response

    async def _await_controlled[T](
        self,
        awaitable: Awaitable[T],
        *,
        control: ProviderControl | None,
        deadline: float,
    ) -> T:
        task = asyncio.ensure_future(awaitable)
        try:
            while True:
                self._check_control(control)
                remaining = deadline - self.clock()
                if remaining <= 0:
                    await _cancel_and_wait(task)
                    raise self._deadline_error()
                done, _ = await asyncio.wait(
                    {task},
                    timeout=min(remaining, _CONTROL_POLL_SECONDS),
                )
                if done:
                    return task.result()
        except asyncio.CancelledError:
            await _cancel_and_wait(task)
            raise
        except BaseException:
            await _cancel_and_wait(task)
            raise

    async def _wait_backoff(
        self,
        delay: float,
        *,
        control: ProviderControl | None,
        deadline: float,
    ) -> None:
        if control is None:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise self._deadline_error()
            await self.sleep(min(delay, remaining))
            if delay >= remaining:
                raise self._deadline_error()
            return

        backoff_deadline = self.clock() + delay
        while True:
            self._check_control(control)
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise self._deadline_error()
            backoff_remaining = backoff_deadline - self.clock()
            if backoff_remaining <= 0:
                return
            await self.sleep(min(remaining, backoff_remaining, _CONTROL_POLL_SECONDS))

    def _check_control(self, control: ProviderControl | None) -> None:
        if control is None:
            return
        try:
            action = control()
        except Exception:
            raise ProviderError(
                ProviderErrorKind.OBSERVER,
                "provider execution control callback failed",
            ) from None
        if action is not None:
            raise TransportControlError(action)

    def _should_retry(self, error: ProviderError) -> bool:
        if error.kind is ProviderErrorKind.CANCELLED or error.partial_output:
            return False
        if error.http_status in _RETRIABLE_STATUS_CODES:
            return error.retryable
        return (
            error.retryable
            and not error.request_sent
            and error.kind in {ProviderErrorKind.CONNECTION, ProviderErrorKind.TIMEOUT}
        )

    def _retry_delay(self, attempt_number: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(max(retry_after, 0.0), _MAX_RETRY_AFTER_SECONDS)
        base = float(2**attempt_number)
        try:
            jitter = self.random_source()
        except Exception:
            jitter = 0.0
        if not math.isfinite(jitter):
            jitter = 0.0
        jitter = min(max(jitter, 0.0), 1.0)
        return min(base * (1.0 + jitter * 0.1), _MAX_RETRY_AFTER_SECONDS)

    def _retry_after(self, value: str | None) -> float | None:
        if value is None:
            return None
        try:
            seconds = float(value)
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            now = self.wall_clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)
            seconds = (date - now).total_seconds()
        if not math.isfinite(seconds):
            return None
        return min(max(seconds, 0.0), _MAX_RETRY_AFTER_SECONDS)

    def _deadline_error(self) -> ProviderError:
        return ProviderError(
            ProviderErrorKind.TIMEOUT,
            "provider request exceeded its total time limit",
            request_sent=True,
            retryable=False,
            usage_unknown=True,
        )


class LegacyProviderAdapter:
    """Adapt a legacy synchronous ModelProvider without claiming live cancellation."""

    supports_live_cancellation = False

    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    def complete_request(
        self,
        request: ProviderRequest,
        *,
        control: ProviderControl | None = None,
        on_event: ProviderEventObserver | None = None,
    ) -> ModelResponse:
        publisher = _EventPublisher(request.request_id, on_event)
        attempt_id = _attempt_id()
        try:
            publisher.emit(ProviderEventType.REQUEST_STARTED)
            if request.tools and request.output_schema is not None:
                raise ProviderError(
                    ProviderErrorKind.CAPABILITY,
                    "structured output cannot be combined with tools",
                )
            if request.output_schema is not None:
                _check_schema(request.output_schema)
            _check_legacy_control(control)
            publisher.emit(ProviderEventType.ATTEMPT_STARTED, attempt_id=attempt_id)
            response = self.provider.complete(list(request.messages), list(request.tools))
            _check_legacy_control(control)
            response = response.model_copy(update={"request_id": request.request_id})
            if (response.finish_reason or "").lower() in {
                "length",
                "incomplete",
                "cancelled",
                "canceled",
            }:
                raise ProviderError(
                    ProviderErrorKind.TRUNCATED,
                    "legacy provider response ended before generation completed",
                    request_sent=True,
                    partial_output=bool(response.content or response.tool_calls),
                    usage_unknown=True,
                )
            if not response.content.strip() and not response.tool_calls:
                raise ProviderError(
                    ProviderErrorKind.PROTOCOL,
                    "legacy provider returned an empty response",
                    request_sent=True,
                    usage_unknown=True,
                )
            if request.output_schema is not None:
                _validate_structured_output(request.output_schema, response.content)
            allowed_tools = {tool.name for tool in request.tools}
            seen_ids: set[str] = set()
            for call in response.tool_calls:
                if (
                    not call.id.strip()
                    or call.id == "missing-tool-call-id"
                    or call.id in seen_ids
                    or call.name not in allowed_tools
                ):
                    raise ProviderError(
                        ProviderErrorKind.PROTOCOL,
                        "legacy provider returned an invalid tool call",
                        request_sent=True,
                        usage_unknown=True,
                    )
                seen_ids.add(call.id)
            if _usage_is_present(response.usage):
                publisher.emit(
                    ProviderEventType.USAGE,
                    attempt_id=attempt_id,
                    usage=response.usage,
                )
            publisher.emit(
                ProviderEventType.RESPONSE_COMPLETED,
                attempt_id=attempt_id,
                response=response,
            )
            return response
        except TransportControlError:
            publisher.emit(ProviderEventType.REQUEST_CANCELLED)
            raise
        except ProviderError as error:
            if error.kind is ProviderErrorKind.OBSERVER:
                raise
            publisher.emit(ProviderEventType.REQUEST_FAILED, error=error)
            raise
        except Exception:
            legacy_error = ProviderError(
                ProviderErrorKind.CONNECTION,
                "legacy provider request failed",
                request_sent=True,
                usage_unknown=True,
            )
            publisher.emit(ProviderEventType.REQUEST_FAILED, error=legacy_error)
            raise legacy_error from None

    def complete(self, messages: list[ModelMessage], tools: list[ToolSpec]) -> ModelResponse:
        request = ProviderRequest(
            request_id=f"legacy-{uuid4().hex}",
            task_id="legacy",
            step_index=0,
            purpose=ProviderRequestPurpose.AGENT_STEP,
            messages=tuple(messages),
            tools=tuple(tools),
        )
        return self.complete_request(request)


def _attempt_id() -> str:
    return f"attempt-{uuid4().hex}"


def _estimated_context_tokens(request: ProviderRequest, output_tokens: int) -> int:
    projection = {
        "messages": [message.model_dump(mode="json") for message in request.messages],
        "tools": [tool.model_dump(mode="json") for tool in request.tools],
        "output_schema": request.output_schema,
    }
    serialized = json.dumps(
        projection,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(serialized) + 64 * len(request.messages) + output_tokens


def _media_type(headers: Mapping[str, str]) -> str:
    content_type = headers.get("content-type", "")
    return content_type.partition(";")[0].strip().lower()


async def _read_json_object(response: TransportResponse, max_bytes: int) -> dict[str, Any]:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise ProviderError(
                ProviderErrorKind.RESPONSE_TOO_LARGE,
                "provider response exceeded the configured size limit",
                request_sent=True,
                partial_output=True,
                usage_unknown=True,
            )
    try:
        parsed = json.loads(body.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ProviderError(
            ProviderErrorKind.PROTOCOL,
            "provider returned invalid JSON",
            request_sent=True,
            partial_output=bool(body),
            usage_unknown=True,
        ) from None
    if not isinstance(parsed, dict):
        raise ProviderError(
            ProviderErrorKind.PROTOCOL,
            "provider JSON response must be an object",
            request_sent=True,
            usage_unknown=True,
        )
    return parsed


async def _discard_body(response: TransportResponse) -> None:
    async for _ in response.aiter_bytes():
        pass


def _http_status_error(
    response: TransportResponse,
    *,
    retry_after: float | None,
) -> ProviderError:
    status_code = response.status_code
    if status_code in {401, 403}:
        kind = ProviderErrorKind.AUTHENTICATION
        message = "provider rejected the configured credentials"
    elif status_code == 429:
        kind = ProviderErrorKind.RATE_LIMIT
        message = "provider rate limit was reached"
    elif status_code >= 500:
        kind = ProviderErrorKind.CONNECTION
        message = f"provider returned HTTP {status_code}"
    else:
        kind = ProviderErrorKind.PROTOCOL
        message = f"provider returned HTTP {status_code}"
    retryable = status_code in _RETRIABLE_STATUS_CODES
    return ProviderError(
        kind,
        message,
        http_status=status_code,
        retry_after=retry_after,
        request_sent=True,
        retryable=retryable,
        usage_unknown=True,
    )


def _usage_is_present(usage: ModelUsage) -> bool:
    return any(
        (
            usage.input_tokens,
            usage.output_tokens,
            usage.cost_usd,
            usage.cache_hit_tokens is not None,
            usage.cache_miss_tokens is not None,
            usage.cache_write_tokens is not None,
            usage.input_tokens_reported is True,
            usage.output_tokens_reported is True,
        )
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _check_schema(schema: dict[str, Any]) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ProviderError(
            ProviderErrorKind.CONFIGURATION,
            "the structured output schema is invalid",
        ) from None


def _check_legacy_control(control: ProviderControl | None) -> None:
    if control is None:
        return
    try:
        action = control()
    except Exception:
        raise ProviderError(
            ProviderErrorKind.OBSERVER,
            "provider execution control callback failed",
        ) from None
    if action is not None:
        raise TransportControlError(action)


def _validate_structured_output(schema: dict[str, Any], content: str) -> None:
    try:
        output = json.loads(content, parse_constant=_reject_json_constant)
        Draft202012Validator(schema).validate(output)
    except (json.JSONDecodeError, ValidationError, ValueError):
        raise ProviderError(
            ProviderErrorKind.STRUCTURED_OUTPUT_INVALID,
            "legacy provider response was not valid JSON for the requested schema",
            request_sent=True,
            usage_unknown=True,
        ) from None


async def _cancel_and_wait(task: asyncio.Future[Any]) -> None:
    if not task.done():
        task.cancel()
    with suppress(BaseException):
        await task
