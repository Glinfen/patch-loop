"""OpenAI Responses protocol adapter and strict SSE reducer."""

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
    ProviderBinding,
    ProviderContinuation,
    ProviderError,
    ProviderErrorKind,
    ProviderProtocol,
    ValidatedResponseItem,
)
from patchloop.providers.sse import SSEFrame
from patchloop.providers.usage import UsageNormalizer

_ITEM_FIELDS: dict[str, frozenset[str]] = {
    "message": frozenset({"id", "type", "status", "role", "content"}),
    "function_call": frozenset({"id", "type", "status", "call_id", "name", "arguments"}),
    "reasoning": frozenset({"id", "type", "status", "summary", "encrypted_content"}),
}
_MESSAGE_CONTENT_FIELDS = frozenset({"type", "text", "refusal", "annotations", "logprobs"})
_PROGRESS_EVENTS = frozenset(
    {
        "response.created",
        "response.in_progress",
        "response.queued",
        "response.content_part.added",
        "response.content_part.done",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
        "response.output_text.annotation.added",
    }
)


class ResponsesAdapter(ProviderAdapter):
    """Translate the Responses API into PatchLoop's provider-neutral contract."""

    def encode(self, request: ProviderRequest, binding: ProviderBinding) -> EncodedRequest:
        self._validate_binding(binding)
        input_items: list[dict[str, Any]] = []
        for message in request.messages:
            input_items.extend(_encode_message(message))

        streaming = binding.transport.streaming and binding.capabilities.streaming
        output_tokens = request.max_output_tokens or binding.generation.max_output_tokens
        payload: dict[str, Any] = {
            "model": binding.model,
            "input": input_items,
            "store": False,
            "stream": streaming,
            "max_output_tokens": output_tokens,
        }
        if binding.generation.temperature is not None:
            payload["temperature"] = binding.generation.temperature
        if binding.generation.reasoning_enabled:
            if binding.generation.reasoning_effort is not None:
                payload["reasoning"] = {"effort": binding.generation.reasoning_effort}
            payload["include"] = ["reasoning.encrypted_content"]
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "strict": False,
                }
                for tool in request.tools
            ]
            if request.purpose is ProviderRequestPurpose.EPOCH_COMPRESSION:
                payload["tool_choice"] = "none"
        if not binding.capabilities.multiple_tool_calls:
            payload["parallel_tool_calls"] = False
        if request.output_schema is not None:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "patchloop_response",
                    "schema": request.output_schema,
                    "strict": True,
                }
            }

        return EncodedRequest(
            path="/responses",
            headers={
                "accept": "text/event-stream" if streaming else "application/json",
                "content-type": "application/json",
            },
            body=payload,
            stream=streaming,
        )

    def parse_json(self, body: dict[str, Any], binding: ProviderBinding) -> ModelResponse:
        self._validate_binding(binding)
        if body.get("error") is not None:
            raise _error(ProviderErrorKind.CONNECTION, "provider reported a service error")
        return _parse_completed_response(body, binding)

    def new_reducer(self, binding: ProviderBinding) -> ResponsesStreamReducer:
        self._validate_binding(binding)
        return ResponsesStreamReducer(binding)

    @staticmethod
    def _validate_binding(binding: ProviderBinding) -> None:
        if binding.protocol is not ProviderProtocol.RESPONSES:
            raise _error(
                ProviderErrorKind.CONFIGURATION,
                "Responses adapter does not match the selected provider binding",
            )


@dataclass
class _OutputItemState:
    output_index: int
    item_id: str
    item_type: str
    done_item: dict[str, Any] | None = None
    text_deltas: dict[int, list[str]] = field(default_factory=dict)
    text_done: dict[int, str] = field(default_factory=dict)
    argument_deltas: list[str] = field(default_factory=list)
    arguments_done: str | None = None
    reasoning_deltas: list[str] = field(default_factory=list)
    reasoning_done: str | None = None
    tool_call_index: int | None = None


class ResponsesStreamReducer(StreamReducer):
    """Reduce one Responses SSE stream and require its terminal snapshot."""

    def __init__(self, binding: ProviderBinding) -> None:
        self.binding = binding
        self._items: dict[int, _OutputItemState] = {}
        self._next_tool_call_index = 0
        self._completed: ModelResponse | None = None
        self._finished = False

    def feed(self, frame: object) -> list[ProviderEvent]:
        if not isinstance(frame, SSEFrame):
            raise _stream_error("Responses reducer received an invalid SSE frame")
        if self._finished or self._completed is not None:
            raise _stream_error("provider sent data after stream completion")
        data = _parse_json_object(frame.data, "Responses stream frame")
        if data.get("error") is not None:
            raise _error(ProviderErrorKind.CONNECTION, "provider reported a service error")
        event_type = data.get("type", frame.event)
        if frame.event is not None and data.get("type", frame.event) != frame.event:
            raise _stream_error("Responses stream event name did not match its payload")
        if not isinstance(event_type, str):
            raise _stream_error("Responses stream frame omitted its event type")

        if event_type in _PROGRESS_EVENTS:
            return []
        if event_type == "response.output_item.added":
            self._add_item(data)
            return []
        if event_type == "response.output_item.done":
            self._complete_item(data)
            return []
        if event_type == "response.output_text.delta":
            return self._text_delta(data)
        if event_type == "response.output_text.done":
            self._text_done(data)
            return []
        if event_type == "response.function_call_arguments.delta":
            return self._arguments_delta(data)
        if event_type == "response.function_call_arguments.done":
            self._arguments_done(data)
            return []
        if event_type == "response.reasoning_summary_text.delta":
            return self._reasoning_delta(data)
        if event_type == "response.reasoning_summary_text.done":
            self._reasoning_done(data)
            return []
        if event_type == "response.refusal.delta":
            refusal = data.get("delta")
            if not isinstance(refusal, str):
                raise _stream_error("Responses stream contained invalid refusal data")
            if refusal:
                raise _error(
                    ProviderErrorKind.REFUSAL, "provider refused the request", partial=True
                )
            return []
        if event_type == "response.completed":
            response = data.get("response")
            if not isinstance(response, dict):
                raise _stream_error("Responses completion event omitted its response")
            self._verify_terminal_items(response)
            self._completed = _parse_completed_response(
                {"object": "response", **response}, self.binding
            )
            return []
        if event_type == "response.incomplete":
            raise _error(
                ProviderErrorKind.TRUNCATED,
                "provider response ended before generation completed",
                partial=bool(self._items),
            )
        if event_type == "response.failed":
            raise _error(
                ProviderErrorKind.CONNECTION,
                "provider reported a failed response",
                partial=bool(self._items),
            )
        if event_type == "response.error":
            raise _error(
                ProviderErrorKind.CONNECTION,
                "provider reported a service error",
                partial=bool(self._items),
            )
        raise _stream_error("provider sent an unsupported Responses event")

    def finish(self) -> ModelResponse:
        if self._finished:
            raise _stream_error("Responses reducer was finalized more than once")
        self._finished = True
        if self._completed is None:
            raise _error(
                ProviderErrorKind.TRUNCATED,
                "provider stream ended before response.completed",
                partial=bool(self._items),
            )
        return self._completed

    def _add_item(self, data: dict[str, Any]) -> None:
        output_index = _event_index(data)
        item = data.get("item")
        if not isinstance(item, dict):
            raise _stream_error("Responses output item event omitted its item")
        item_type = _item_type(item)
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise _stream_error("Responses output item omitted its item ID")
        if output_index in self._items or any(
            state.item_id == item_id for state in self._items.values()
        ):
            raise _stream_error("Responses stream repeated an output index or item ID")
        tool_call_index = None
        if item_type == "function_call":
            tool_call_index = self._next_tool_call_index
            self._next_tool_call_index += 1
        self._items[output_index] = _OutputItemState(
            output_index=output_index,
            item_id=item_id,
            item_type=item_type,
            tool_call_index=tool_call_index,
        )

    def _complete_item(self, data: dict[str, Any]) -> None:
        output_index = _event_index(data)
        state = self._items.get(output_index)
        if state is None:
            raise _stream_error("Responses completed an output item that was never added")
        if state.done_item is not None:
            raise _stream_error("Responses sent a repeated output item completion")
        item = data.get("item")
        if not isinstance(item, dict):
            raise _stream_error("Responses output item completion omitted its item")
        if _item_type(item) != state.item_type:
            raise _stream_error("Responses output item changed type during streaming")
        try:
            clean = _validated_native_item(item)
        except ProviderError:
            raise
        if clean.get("id") != state.item_id:
            raise _stream_error("Responses output item changed ID during streaming")
        if state.item_type == "message":
            _cross_check_text(state, clean)
        elif state.item_type == "function_call":
            _cross_check_arguments(state, clean)
        else:
            _cross_check_reasoning(state, clean)
        state.done_item = clean

    def _text_delta(self, data: dict[str, Any]) -> list[ProviderEvent]:
        state = self._event_state(data, expected_type="message")
        content_index = _content_index(data)
        delta = data.get("delta")
        if not isinstance(delta, str):
            raise _stream_error("Responses text delta was not text")
        state.text_deltas.setdefault(content_index, []).append(delta)
        if not delta:
            return []
        return [
            _event(ProviderEventType.TEXT_DELTA, delta=delta),
        ]

    def _text_done(self, data: dict[str, Any]) -> None:
        state = self._event_state(data, expected_type="message")
        content_index = _content_index(data)
        text = data.get("text")
        if not isinstance(text, str):
            raise _stream_error("Responses completed text snapshot was not text")
        previous = state.text_done.get(content_index)
        if previous is not None and previous != text:
            raise _stream_error("Responses sent conflicting completed text snapshots")
        state.text_done[content_index] = text
        if content_index in state.text_deltas and "".join(state.text_deltas[content_index]) != text:
            raise _stream_error("Responses text delta did not match its completed snapshot")

    def _arguments_delta(self, data: dict[str, Any]) -> list[ProviderEvent]:
        state = self._event_state(data, expected_type="function_call")
        delta = data.get("delta")
        if not isinstance(delta, str):
            raise _stream_error("Responses function arguments delta was not text")
        state.argument_deltas.append(delta)
        if not delta:
            return []
        return [
            _event(
                ProviderEventType.TOOL_CALL_DELTA,
                delta=delta,
                tool_call_index=state.tool_call_index,
            )
        ]

    def _arguments_done(self, data: dict[str, Any]) -> None:
        state = self._event_state(data, expected_type="function_call")
        arguments = data.get("arguments")
        if not isinstance(arguments, str):
            raise _stream_error("Responses function arguments snapshot was not text")
        if state.arguments_done is not None and state.arguments_done != arguments:
            raise _stream_error("Responses sent conflicting function argument snapshots")
        state.arguments_done = arguments
        if state.argument_deltas and "".join(state.argument_deltas) != arguments:
            raise _stream_error("Responses argument deltas did not match their completed snapshot")

    def _reasoning_delta(self, data: dict[str, Any]) -> list[ProviderEvent]:
        state = self._event_state(data, expected_type="reasoning")
        delta = data.get("delta")
        if not isinstance(delta, str):
            raise _stream_error("Responses reasoning summary delta was not text")
        state.reasoning_deltas.append(delta)
        if not delta:
            return []
        return [_event(ProviderEventType.REASONING_DELTA, delta=delta)]

    def _reasoning_done(self, data: dict[str, Any]) -> None:
        state = self._event_state(data, expected_type="reasoning")
        text = data.get("text")
        if not isinstance(text, str):
            raise _stream_error("Responses reasoning summary snapshot was not text")
        if state.reasoning_done is not None and state.reasoning_done != text:
            raise _stream_error("Responses sent conflicting reasoning summary snapshots")
        state.reasoning_done = text
        if state.reasoning_deltas and "".join(state.reasoning_deltas) != text:
            raise _stream_error("Responses reasoning deltas did not match their completed snapshot")

    def _event_state(
        self,
        data: dict[str, Any],
        *,
        expected_type: str | None = None,
    ) -> _OutputItemState:
        output_index = _event_index(data)
        item_id = data.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            raise _stream_error("Responses output event omitted its item ID")
        state = self._items.get(output_index)
        if state is None or state.item_id != item_id:
            raise _stream_error("Responses output event referred to an unknown item")
        if expected_type is not None and state.item_type != expected_type:
            raise _stream_error("Responses output event referred to an item of the wrong type")
        if state.done_item is not None:
            raise _stream_error("Responses sent an output event after its item was completed")
        return state

    def _verify_terminal_items(self, response: dict[str, Any]) -> None:
        if response.get("status") != "completed":
            _raise_response_status(response.get("status"), partial=bool(self._items))
        output = response.get("output")
        if not isinstance(output, list):
            raise _stream_error("Responses completion omitted its output list")
        if len(output) != len(self._items) or set(self._items) != set(range(len(output))):
            raise _stream_error("Responses completion output did not match streamed items")
        for index, raw_item in enumerate(output):
            state = self._items[index]
            if state.done_item is None or not isinstance(raw_item, dict):
                raise _stream_error(
                    "Responses completion arrived before every output item was done"
                )
            final_item = _validated_native_item(raw_item)
            if final_item != state.done_item:
                raise _stream_error("Responses completed output differed from its item snapshot")


def _encode_message(message: Any) -> list[dict[str, Any]]:
    role = message.role
    if role == "tool":
        if not isinstance(message.tool_call_id, str) or not message.tool_call_id:
            raise _error(ProviderErrorKind.PROTOCOL, "tool result omitted its function call ID")
        if message.continuation is not None:
            raise _error(
                ProviderErrorKind.CONTINUATION_UNAVAILABLE,
                "tool result cannot carry a Responses continuation",
            )
        return [
            {
                "type": "function_call_output",
                "call_id": message.tool_call_id,
                "output": message.content,
            }
        ]
    if role not in {"system", "developer", "user", "assistant"}:
        raise _error(ProviderErrorKind.PROTOCOL, "request contains an unsupported message role")

    if message.continuation is not None:
        if role != "assistant" or not message.continuation.responses_items:
            raise _error(
                ProviderErrorKind.CONTINUATION_UNAVAILABLE,
                "assistant history has no replayable Responses continuation items",
            )
        if not message.continuation.replayable:
            raise _error(
                ProviderErrorKind.CONTINUATION_UNAVAILABLE,
                "stored Responses continuation was redacted and cannot be replayed",
            )
        replayed: list[dict[str, Any]] = []
        for item in message.continuation.responses_items:
            try:
                clean = _validated_native_item(item.item)
            except ProviderError:
                raise _error(
                    ProviderErrorKind.CONTINUATION_UNAVAILABLE,
                    "stored Responses continuation could not be replayed safely",
                ) from None
            if clean["type"] != item.type:
                raise _error(
                    ProviderErrorKind.CONTINUATION_UNAVAILABLE,
                    "stored Responses continuation item type did not match its payload",
                )
            replayed.append(clean)
        return replayed

    items: list[dict[str, Any]] = []
    if message.content or not message.tool_calls:
        items.append({"type": "message", "role": role, "content": message.content})
    for call in message.tool_calls:
        if not call.id or not call.name:
            raise _error(
                ProviderErrorKind.PROTOCOL, "assistant function call omitted its ID or name"
            )
        items.append(
            {
                "type": "function_call",
                "call_id": call.id,
                "name": call.name,
                "arguments": _encode_arguments(call.arguments),
            }
        )
    return items


def _parse_completed_response(body: dict[str, Any], binding: ProviderBinding) -> ModelResponse:
    if body.get("error") is not None:
        raise _error(ProviderErrorKind.CONNECTION, "provider reported a service error")
    if body.get("object") != "response":
        raise _error(ProviderErrorKind.PROTOCOL, "provider returned an invalid Responses object")
    status = body.get("status")
    if status != "completed":
        _raise_response_status(status, partial=bool(body.get("output")))
    output = body.get("output")
    if not isinstance(output, list):
        raise _error(ProviderErrorKind.PROTOCOL, "provider response omitted its output items")

    text_parts: list[str] = []
    calls: list[ToolCall] = []
    native_items: list[ValidatedResponseItem] = []
    seen_item_ids: set[str] = set()
    for raw_item in output:
        if not isinstance(raw_item, dict):
            raise _error(ProviderErrorKind.PROTOCOL, "provider returned an invalid output item")
        item = _validated_native_item(raw_item)
        if item["id"] in seen_item_ids:
            raise _error(ProviderErrorKind.PROTOCOL, "provider repeated a Responses output item ID")
        seen_item_ids.add(item["id"])
        kind = item["type"]
        native_items.append(ValidatedResponseItem(type=kind, item=item))
        if kind == "message":
            if item.get("role") != "assistant":
                raise _error(
                    ProviderErrorKind.PROTOCOL, "provider returned a non-assistant output message"
                )
            content = item.get("content")
            if not isinstance(content, list):
                raise _error(
                    ProviderErrorKind.PROTOCOL, "provider returned invalid message content"
                )
            for part in content:
                part_type = part.get("type")
                if part_type == "refusal":
                    raise _error(ProviderErrorKind.REFUSAL, "provider refused the request")
                if part_type != "output_text":
                    raise _error(
                        ProviderErrorKind.UNSUPPORTED_OUTPUT,
                        "provider returned an unsupported message output item",
                    )
                text_parts.append(part["text"])
        elif kind == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            raw_arguments = item.get("arguments")
            if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                raise _error(
                    ProviderErrorKind.PROTOCOL, "provider returned an invalid function call"
                )
            arguments, arguments_error = _parse_arguments(raw_arguments)
            calls.append(
                ToolCall(
                    id=call_id,
                    name=name,
                    arguments=arguments,
                    arguments_error=arguments_error,
                )
            )

    continuation = (
        ProviderContinuation(responses_items=tuple(native_items)) if native_items else None
    )
    return ModelResponse(
        content="".join(text_parts),
        tool_calls=calls,
        continuation=continuation,
        usage=_parse_usage(body.get("usage"), binding.pricing),
        finish_reason="completed",
    )


def _validated_native_item(raw_item: dict[str, Any]) -> dict[str, Any]:
    item_type = _item_type(raw_item)
    unknown = set(raw_item) - _ITEM_FIELDS[item_type]
    if unknown:
        raise _error(
            ProviderErrorKind.PROTOCOL,
            "provider returned an output item with unsupported fields",
        )
    item = dict(raw_item)
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id:
        raise _error(ProviderErrorKind.PROTOCOL, "provider output item omitted its item ID")
    status = item.get("status")
    if status is not None and status != "completed":
        _raise_response_status(status, partial=True)
    if item_type == "message":
        if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
            raise _error(ProviderErrorKind.PROTOCOL, "provider returned an invalid output message")
        clean_parts: list[dict[str, Any]] = []
        for part in item["content"]:
            if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                raise _error(
                    ProviderErrorKind.PROTOCOL, "provider returned invalid message content"
                )
            part_type = part["type"]
            if part_type not in {"output_text", "refusal"}:
                raise _error(
                    ProviderErrorKind.UNSUPPORTED_OUTPUT,
                    "provider returned an unsupported message output item",
                )
            if set(part) - _MESSAGE_CONTENT_FIELDS:
                raise _error(
                    ProviderErrorKind.PROTOCOL, "provider returned unsupported content fields"
                )
            if part_type == "refusal":
                if not isinstance(part.get("refusal"), str):
                    raise _error(
                        ProviderErrorKind.PROTOCOL,
                        "provider returned invalid refusal data",
                    )
            elif not isinstance(part.get("text"), str):
                raise _error(
                    ProviderErrorKind.PROTOCOL, "provider returned non-text message content"
                )
            clean_parts.append(dict(part))
        item["content"] = clean_parts
    elif item_type == "function_call":
        for field_name in ("call_id", "name", "arguments"):
            if not isinstance(item.get(field_name), str):
                raise _error(
                    ProviderErrorKind.PROTOCOL, "provider returned an invalid function call"
                )
        if not item["call_id"] or not item["name"]:
            raise _error(
                ProviderErrorKind.PROTOCOL, "provider returned an incomplete function call"
            )
    else:
        summary = item.get("summary", [])
        if not isinstance(summary, list):
            raise _error(
                ProviderErrorKind.PROTOCOL, "provider returned invalid reasoning summary data"
            )
        for part in summary:
            if (
                not isinstance(part, dict)
                or part.get("type") != "summary_text"
                or not isinstance(part.get("text"), str)
                or set(part) - {"type", "text"}
            ):
                raise _error(
                    ProviderErrorKind.PROTOCOL, "provider returned invalid reasoning summary data"
                )
        encrypted = item.get("encrypted_content")
        if not isinstance(encrypted, str) or not encrypted:
            raise _error(
                ProviderErrorKind.CONTINUATION_UNAVAILABLE,
                "provider omitted encrypted reasoning continuation data",
            )
    return item


def _cross_check_text(state: _OutputItemState, item: dict[str, Any]) -> None:
    content = item["content"]
    texts = {
        index: part["text"] for index, part in enumerate(content) if part["type"] == "output_text"
    }
    for index, deltas in state.text_deltas.items():
        if texts.get(index) != "".join(deltas):
            raise _stream_error("Responses text deltas did not match completed output")
    for index, text in state.text_done.items():
        if texts.get(index) != text:
            raise _stream_error("Responses text snapshot did not match completed output")


def _cross_check_arguments(state: _OutputItemState, item: dict[str, Any]) -> None:
    arguments = item["arguments"]
    if state.argument_deltas and "".join(state.argument_deltas) != arguments:
        raise _stream_error("Responses function deltas did not match completed output")
    if state.arguments_done is not None and state.arguments_done != arguments:
        raise _stream_error("Responses function snapshot did not match completed output")


def _cross_check_reasoning(state: _OutputItemState, item: dict[str, Any]) -> None:
    summary = "".join(part["text"] for part in item.get("summary", []))
    if state.reasoning_deltas and "".join(state.reasoning_deltas) != summary:
        raise _stream_error("Responses reasoning deltas did not match completed output")
    if state.reasoning_done is not None and state.reasoning_done != summary:
        raise _stream_error("Responses reasoning snapshot did not match completed output")


def _item_type(item: dict[str, Any]) -> str:
    item_type = item.get("type")
    if not isinstance(item_type, str) or item_type not in _ITEM_FIELDS:
        raise _error(
            ProviderErrorKind.UNSUPPORTED_OUTPUT,
            "provider returned an unsupported output item type",
        )
    return item_type


def _parse_usage(raw_usage: Any, pricing: Any) -> ModelUsage:
    return UsageNormalizer.normalize(ProviderProtocol.RESPONSES, raw_usage, pricing)


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


def _encode_arguments(arguments: dict[str, Any]) -> str:
    try:
        return json.dumps(arguments, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raise _error(
            ProviderErrorKind.PROTOCOL, "assistant function arguments are not valid JSON"
        ) from None


def _raise_response_status(status: Any, *, partial: bool) -> None:
    if status == "incomplete":
        raise _error(
            ProviderErrorKind.TRUNCATED,
            "provider response ended before generation completed",
            partial=partial,
        )
    if status == "failed":
        raise _error(
            ProviderErrorKind.CONNECTION, "provider reported a failed response", partial=partial
        )
    if isinstance(status, str) and status in {"cancelled", "canceled"}:
        raise _error(
            ProviderErrorKind.TRUNCATED, "provider response was cancelled", partial=partial
        )
    raise _error(ProviderErrorKind.PROTOCOL, "provider response was not completed", partial=partial)


def _parse_json_object(raw: str, description: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError):
        raise _stream_error(f"provider returned invalid {description} JSON") from None
    if not isinstance(value, dict):
        raise _stream_error(f"provider returned a non-object {description}")
    return value


def _event_index(data: dict[str, Any]) -> int:
    value = data.get("output_index")
    if not _is_nonnegative_int(value):
        raise _stream_error("Responses output event omitted a valid output index")
    return value


def _content_index(data: dict[str, Any]) -> int:
    value = data.get("content_index")
    if not _is_nonnegative_int(value):
        raise _stream_error("Responses text event omitted a valid content index")
    return value


def _event(
    event_type: ProviderEventType,
    *,
    delta: str,
    tool_call_index: int | None = None,
) -> ProviderEvent:
    return ProviderEvent(
        type=event_type,
        request_id="responses-stream",
        sequence=0,
        delta=delta,
        tool_call_index=tool_call_index,
    )


def _unknown_usage() -> ModelUsage:
    return UsageNormalizer.unknown()


def _is_nonnegative_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _error(
    kind: ProviderErrorKind,
    message: str,
    *,
    partial: bool = False,
) -> ProviderError:
    return ProviderError(
        kind,
        message,
        request_sent=True,
        partial_output=partial,
        usage_unknown=True,
    )


def _stream_error(message: str) -> ProviderError:
    return _error(ProviderErrorKind.PROTOCOL, message)
