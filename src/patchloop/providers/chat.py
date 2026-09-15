"""OpenAI-compatible Chat Completions adapter with a DeepSeek dialect."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, TypeGuard

from patchloop.domain import ToolCall
from patchloop.providers.base import (
    EncodedRequest,
    ModelResponse,
    ModelUsage,
    ProviderAdapter,
    ProviderEvent,
    ProviderEventType,
    ProviderRequest,
    ProviderRequestPurpose,
    StreamReducer,
)
from patchloop.providers.contracts import (
    ChatDialect,
    ProviderBinding,
    ProviderContinuation,
    ProviderError,
    ProviderErrorKind,
    ProviderProtocol,
)
from patchloop.providers.sse import SSEFrame
from patchloop.providers.usage import UsageNormalizer


class ChatCompletionsAdapter(ProviderAdapter):
    """Encode and parse Chat Completions for standard or DeepSeek profiles."""

    def __init__(self, dialect: ChatDialect | str = ChatDialect.STANDARD) -> None:
        self.dialect = ChatDialect(dialect)

    def encode(self, request: ProviderRequest, binding: ProviderBinding) -> EncodedRequest:
        self._validate_binding(binding)
        if self.dialect is ChatDialect.DEEPSEEK and binding.generation.reasoning_enabled:
            _validate_deepseek_history(request)

        streaming = binding.transport.streaming and binding.capabilities.streaming
        output_tokens = request.max_output_tokens or binding.generation.max_output_tokens
        payload: dict[str, Any] = {
            "model": binding.model,
            "messages": [_message_payload(message, self.dialect) for message in request.messages],
            binding.generation.token_limit_field: output_tokens,
            "stream": streaming,
        }
        if binding.generation.temperature is not None:
            payload["temperature"] = binding.generation.temperature

        if request.tools:
            payload["tools"] = [_tool_payload(tool) for tool in request.tools]
            payload["tool_choice"] = (
                "none" if request.purpose is ProviderRequestPurpose.EPOCH_COMPRESSION else "auto"
            )

        if self.dialect is ChatDialect.DEEPSEEK:
            payload["thinking"] = {
                "type": "enabled" if binding.generation.reasoning_enabled else "disabled"
            }
        if binding.generation.reasoning_effort is not None:
            payload["reasoning_effort"] = binding.generation.reasoning_effort

        if request.output_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "patchloop_response",
                    "strict": True,
                    "schema": request.output_schema,
                },
            }

        if streaming and binding.capabilities.usage_supported:
            payload["stream_options"] = {"include_usage": True}

        return EncodedRequest(
            path="/chat/completions",
            headers={
                "accept": "text/event-stream" if streaming else "application/json",
                "content-type": "application/json",
            },
            body=payload,
            stream=streaming,
        )

    def parse_json(self, body: dict[str, Any], binding: ProviderBinding) -> ModelResponse:
        self._validate_binding(binding)
        if "error" in body:
            raise _provider_error(
                ProviderErrorKind.CONNECTION,
                "provider reported a service error",
            )
        choices = body.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise _provider_error(ProviderErrorKind.PROTOCOL, "provider returned invalid choices")
        choice = choices[0]
        index = choice.get("index", 0)
        if not _is_nonnegative_int(index) or index != 0:
            raise _provider_error(
                ProviderErrorKind.PROTOCOL, "provider returned an unsupported choice"
            )
        message = choice.get("message")
        if not isinstance(message, dict):
            raise _provider_error(ProviderErrorKind.PROTOCOL, "provider response has no message")

        finish_reason = _finish_reason(choice.get("finish_reason"))
        refusal = message.get("refusal")
        if refusal is not None:
            if not isinstance(refusal, str):
                raise _provider_error(
                    ProviderErrorKind.PROTOCOL, "provider returned invalid refusal data"
                )
            if refusal:
                raise _provider_error(ProviderErrorKind.REFUSAL, "provider refused the request")

        raw_content = message.get("content", "")
        if raw_content is None:
            content = ""
        elif isinstance(raw_content, str):
            content = raw_content
        else:
            raise _provider_error(
                ProviderErrorKind.PROTOCOL, "provider returned unsupported content"
            )

        raw_reasoning = message.get("reasoning_content")
        reasoning_content: str | None = None
        if self.dialect is ChatDialect.DEEPSEEK and raw_reasoning is not None:
            if not isinstance(raw_reasoning, str):
                raise _provider_error(
                    ProviderErrorKind.PROTOCOL,
                    "provider returned invalid reasoning continuation",
                )
            reasoning_content = raw_reasoning
        if (
            self.dialect is ChatDialect.DEEPSEEK
            and binding.generation.reasoning_enabled
            and reasoning_content is None
        ):
            raise _provider_error(
                ProviderErrorKind.CONTINUATION_UNAVAILABLE,
                "provider response omitted required reasoning continuation",
            )

        calls = _parse_tool_calls(message.get("tool_calls", []))
        _validate_finish_and_calls(finish_reason, calls)
        continuation = (
            ProviderContinuation(deepseek_reasoning_content=reasoning_content)
            if reasoning_content is not None
            else None
        )
        usage = _parse_usage(body.get("usage"), binding.pricing)
        return ModelResponse(
            content=content,
            tool_calls=calls,
            continuation=continuation,
            usage=usage,
            finish_reason=finish_reason,
        )

    def new_reducer(self, binding: ProviderBinding) -> ChatStreamReducer:
        self._validate_binding(binding)
        return ChatStreamReducer(self.dialect, binding)

    def _validate_binding(self, binding: ProviderBinding) -> None:
        if (
            binding.protocol is not ProviderProtocol.CHAT_COMPLETIONS
            or binding.dialect is not self.dialect
        ):
            raise ProviderError(
                ProviderErrorKind.CONFIGURATION,
                "Chat adapter does not match the selected provider binding",
            )


@dataclass
class _ToolCallAccumulator:
    call_id: str | None = None
    name_fragments: list[str] = field(default_factory=list)
    argument_fragments: list[str] = field(default_factory=list)

    def add(self, fragment: dict[str, Any]) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        if "id" in fragment:
            call_id = fragment["id"]
            if not isinstance(call_id, str) or not call_id:
                raise _stream_error("provider streamed an invalid tool call ID")
            if self.call_id is not None and self.call_id != call_id:
                raise _stream_error("provider changed a tool call ID during streaming")
            self.call_id = call_id

        call_type = fragment.get("type")
        if call_type is not None and call_type != "function":
            raise _stream_error("provider streamed an unsupported tool call type")

        function = fragment.get("function", {})
        if not isinstance(function, dict):
            raise _stream_error("provider streamed an invalid tool function")
        name = function.get("name")
        if name is not None:
            if not isinstance(name, str):
                raise _stream_error("provider streamed an invalid tool name")
            if name:
                self.name_fragments.append(name)
                events.append(("name", name))

        arguments = function.get("arguments")
        if arguments is not None:
            if not isinstance(arguments, str):
                raise _stream_error("provider streamed non-text tool arguments")
            self.argument_fragments.append(arguments)
            if arguments:
                events.append(("arguments", arguments))
        return events

    def to_tool_call(self) -> ToolCall:
        name = "".join(self.name_fragments)
        if not self.call_id or not name:
            raise _stream_error("provider completed a tool call without an ID or name")
        raw_arguments = "".join(self.argument_fragments) or "{}"
        arguments, arguments_error = _parse_arguments(raw_arguments)
        return ToolCall(
            id=self.call_id,
            name=name,
            arguments=arguments,
            arguments_error=arguments_error,
        )


class ChatStreamReducer(StreamReducer):
    """Reduce one Chat Completions SSE attempt into a complete response."""

    def __init__(self, dialect: ChatDialect, binding: ProviderBinding) -> None:
        self.dialect = dialect
        self.binding = binding
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._saw_reasoning = False
        self._tool_calls: dict[int, _ToolCallAccumulator] = {}
        self._usage = _unknown_usage()
        self._usage_received = False
        self._finish_reason: str | None = None
        self._done = False
        self._finished = False

    def feed(self, frame: object) -> list[ProviderEvent]:
        if not isinstance(frame, SSEFrame):
            raise _stream_error("Chat reducer received an invalid SSE frame")
        if self._finished or self._done:
            raise _stream_error("provider sent data after stream completion")
        if frame.data == "[DONE]":
            self._done = True
            return []

        data = _parse_json_object(frame.data, "provider stream frame")
        if "error" in data:
            raise _provider_error(
                ProviderErrorKind.CONNECTION,
                "provider reported a service error",
                partial_output=bool(self._content or self._reasoning or self._tool_calls),
            )

        events: list[ProviderEvent] = []
        if "usage" in data:
            self._usage = _parse_usage(data["usage"], self.binding.pricing)
            self._usage_received = True
            if _usage_was_reported(self._usage):
                events.append(
                    ProviderEvent(
                        type=ProviderEventType.USAGE,
                        request_id="chat-stream",
                        sequence=0,
                        usage=self._usage,
                    )
                )

        choices = data.get("choices")
        if not isinstance(choices, list):
            raise _stream_error("provider stream frame has no choices list")
        if not choices:
            if "usage" not in data:
                raise _stream_error("provider sent an empty choices frame without usage")
            if self._finish_reason is None:
                raise _stream_error("provider sent usage-only data before finish_reason")
            return events
        if self._finish_reason is not None:
            raise _stream_error("provider sent a choice after finish_reason")
        if len(choices) != 1 or not isinstance(choices[0], dict):
            raise _stream_error("provider streamed an unsupported choice structure")

        choice = choices[0]
        index = choice.get("index", 0)
        if not _is_nonnegative_int(index) or index != 0:
            raise _stream_error("provider streamed an unsupported choice index")
        delta = choice.get("delta", {})
        if not isinstance(delta, dict):
            raise _stream_error("provider streamed an invalid delta")
        role = delta.get("role")
        if role is not None and role != "assistant":
            raise _stream_error("provider streamed an unsupported message role")

        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise _stream_error("provider streamed unsupported content")
            if content:
                self._content.append(content)
                events.append(
                    ProviderEvent(
                        type=ProviderEventType.TEXT_DELTA,
                        request_id="chat-stream",
                        sequence=0,
                        delta=content,
                    )
                )

        reasoning = delta.get("reasoning_content")
        if reasoning is not None:
            if self.dialect is not ChatDialect.DEEPSEEK or not isinstance(reasoning, str):
                raise _stream_error("provider streamed unsupported reasoning data")
            self._saw_reasoning = True
            self._reasoning.append(reasoning)
            if reasoning:
                events.append(
                    ProviderEvent(
                        type=ProviderEventType.REASONING_DELTA,
                        request_id="chat-stream",
                        sequence=0,
                        delta=reasoning,
                    )
                )

        refusal = delta.get("refusal")
        if refusal is not None:
            if not isinstance(refusal, str):
                raise _stream_error("provider streamed invalid refusal data")
            if refusal:
                raise _provider_error(
                    ProviderErrorKind.REFUSAL,
                    "provider refused the request",
                    partial_output=bool(self._content or self._reasoning or self._tool_calls),
                )

        raw_calls = delta.get("tool_calls", [])
        if raw_calls is None:
            raw_calls = []
        if not isinstance(raw_calls, list):
            raise _stream_error("provider streamed an invalid tool call list")
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                raise _stream_error("provider streamed an invalid tool call")
            call_index = raw_call.get("index")
            if not _is_nonnegative_int(call_index):
                raise _stream_error("provider streamed a tool call without a valid index")
            accumulator = self._tool_calls.setdefault(call_index, _ToolCallAccumulator())
            for fragment_kind, fragment_value in accumulator.add(raw_call):
                del fragment_kind
                events.append(
                    ProviderEvent(
                        type=ProviderEventType.TOOL_CALL_DELTA,
                        request_id="chat-stream",
                        sequence=0,
                        tool_call_index=call_index,
                        delta=fragment_value,
                    )
                )

        if "finish_reason" in choice and choice["finish_reason"] is not None:
            if self._finish_reason is not None:
                raise _stream_error("provider sent multiple finish markers")
            self._finish_reason = _finish_reason(choice["finish_reason"])

        return events

    def finish(self) -> ModelResponse:
        if self._finished:
            raise _stream_error("Chat reducer was finalized more than once")
        self._finished = True
        if not self._done or self._finish_reason is None:
            raise ProviderError(
                ProviderErrorKind.TRUNCATED,
                "provider stream ended before protocol completion",
                request_sent=True,
                partial_output=bool(self._content or self._reasoning or self._tool_calls),
                usage_unknown=True,
            )
        if (
            self.dialect is ChatDialect.DEEPSEEK
            and self.binding.generation.reasoning_enabled
            and not self._saw_reasoning
        ):
            raise ProviderError(
                ProviderErrorKind.CONTINUATION_UNAVAILABLE,
                "provider stream omitted required reasoning continuation",
                request_sent=True,
                partial_output=bool(self._content or self._tool_calls),
                usage_unknown=True,
            )

        calls = [self._tool_calls[index].to_tool_call() for index in sorted(self._tool_calls)]
        _validate_finish_and_calls(self._finish_reason, calls)
        continuation = (
            ProviderContinuation(deepseek_reasoning_content="".join(self._reasoning))
            if self._saw_reasoning
            else None
        )
        usage = self._usage if self._usage_received else _unknown_usage()
        return ModelResponse(
            content="".join(self._content),
            tool_calls=calls,
            continuation=continuation,
            usage=usage,
            finish_reason=self._finish_reason,
        )


def _message_payload(message: Any, dialect: ChatDialect) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ),
                },
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    continuation = message.continuation
    if (
        dialect is ChatDialect.STANDARD
        and continuation is not None
        and continuation.responses_items
    ):
        raise ProviderError(
            ProviderErrorKind.CONTINUATION_UNAVAILABLE,
            "Chat Completions cannot replay Responses continuation items",
        )
    if dialect is ChatDialect.DEEPSEEK and continuation is not None and not continuation.replayable:
        raise ProviderError(
            ProviderErrorKind.CONTINUATION_UNAVAILABLE,
            "stored provider continuation was redacted and cannot be replayed",
        )
    if dialect is ChatDialect.DEEPSEEK and continuation is not None:
        if continuation.deepseek_reasoning_content is not None:
            payload["reasoning_content"] = continuation.deepseek_reasoning_content
        elif continuation.responses_items:
            raise ProviderError(
                ProviderErrorKind.CONTINUATION_UNAVAILABLE,
                "DeepSeek cannot replay Responses continuation items",
            )
    return payload


def _validate_deepseek_history(request: ProviderRequest) -> None:
    for message in request.messages:
        if message.role != "assistant":
            continue
        continuation = message.continuation
        if continuation is None or continuation.deepseek_reasoning_content is None:
            raise ProviderError(
                ProviderErrorKind.CONTINUATION_UNAVAILABLE,
                "assistant history is missing required DeepSeek reasoning continuation",
            )
        if not continuation.replayable:
            raise ProviderError(
                ProviderErrorKind.CONTINUATION_UNAVAILABLE,
                "stored DeepSeek continuation was redacted and cannot be replayed",
            )


def _tool_payload(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": f"[{tool.permission}] {tool.description}",
            "parameters": tool.parameters,
        },
    }


def _parse_tool_calls(raw_calls: Any) -> list[ToolCall]:
    if raw_calls is None:
        return []
    if not isinstance(raw_calls, list):
        raise _provider_error(
            ProviderErrorKind.PROTOCOL, "provider returned an invalid tool call list"
        )
    calls: list[ToolCall] = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            raise _provider_error(
                ProviderErrorKind.PROTOCOL, "provider returned an invalid tool call"
            )
        if raw_call.get("type", "function") != "function":
            raise _provider_error(
                ProviderErrorKind.PROTOCOL, "provider returned an unsupported tool type"
            )
        function = raw_call.get("function")
        if not isinstance(function, dict):
            raise _provider_error(ProviderErrorKind.PROTOCOL, "provider tool call has no function")
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise _provider_error(
                ProviderErrorKind.PROTOCOL, "provider tool call has no function name"
            )
        raw_id = raw_call.get("id")
        call_id = raw_id if isinstance(raw_id, str) else ""
        raw_arguments = function.get("arguments", "{}")
        arguments, arguments_error = _parse_arguments(raw_arguments)
        calls.append(
            ToolCall(
                id=call_id,
                name=name,
                arguments=arguments,
                arguments_error=arguments_error,
            )
        )
    return calls


def _parse_arguments(raw_arguments: Any) -> tuple[dict[str, Any], str | None]:
    if not isinstance(raw_arguments, str):
        return {}, "tool arguments are not a JSON string"
    try:
        value = json.loads(raw_arguments, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        detail = exc.msg if isinstance(exc, json.JSONDecodeError) else "invalid JSON constant"
        return {}, f"invalid tool arguments JSON: {detail}"
    if not isinstance(value, dict):
        return {}, "tool arguments JSON must be an object"
    return value, None


def _parse_usage(raw_usage: Any, pricing: Any) -> ModelUsage:
    return UsageNormalizer.normalize(
        ProviderProtocol.CHAT_COMPLETIONS,
        raw_usage,
        pricing,
        legacy_pricing=(pricing is not None and pricing.version == "legacy-deepseek-default"),
    )


def _unknown_usage() -> ModelUsage:
    return UsageNormalizer.unknown()


def _usage_was_reported(usage: ModelUsage) -> bool:
    return any(
        (
            usage.input_tokens_reported,
            usage.output_tokens_reported,
            usage.cache_hit_tokens is not None,
            usage.cache_miss_tokens is not None,
            usage.cache_write_tokens is not None,
        )
    )


def _finish_reason(raw_reason: Any) -> str:
    if not isinstance(raw_reason, str):
        raise _provider_error(ProviderErrorKind.PROTOCOL, "provider omitted finish_reason")
    if raw_reason == "content_filter":
        raise _provider_error(ProviderErrorKind.REFUSAL, "provider filtered the response")
    if raw_reason in {
        "error",
        "failed",
        "resource_exhausted",
        "resource_unavailable",
        "insufficient_system_resource",
    }:
        raise _provider_error(ProviderErrorKind.CONNECTION, "provider reported a service error")
    if raw_reason not in {"stop", "tool_calls", "length", "cancelled", "canceled", "incomplete"}:
        raise _provider_error(
            ProviderErrorKind.PROTOCOL, "provider returned an unknown finish reason"
        )
    return raw_reason


def _validate_finish_and_calls(finish_reason: str, calls: list[ToolCall]) -> None:
    if finish_reason == "tool_calls" and not calls:
        raise _provider_error(
            ProviderErrorKind.PROTOCOL, "provider finished tool calls without a tool call"
        )
    if finish_reason == "stop" and calls:
        raise _provider_error(
            ProviderErrorKind.PROTOCOL, "provider returned tool calls with a stop finish"
        )


def _parse_json_object(raw: str, description: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError):
        raise _stream_error(f"provider returned invalid {description} JSON") from None
    if not isinstance(value, dict):
        raise _stream_error(f"provider returned a non-object {description}")
    return value


def _is_nonnegative_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _provider_error(
    kind: ProviderErrorKind,
    message: str,
    *,
    partial_output: bool = False,
) -> ProviderError:
    return ProviderError(
        kind,
        message,
        request_sent=True,
        partial_output=partial_output,
        usage_unknown=True,
    )


def _stream_error(message: str) -> ProviderError:
    return _provider_error(ProviderErrorKind.PROTOCOL, message)
