"""DeepSeek V4 Flash provider using the OpenAI-compatible Chat API."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from patchloop.domain import ToolCall
from patchloop.providers.base import (
    ModelMessage,
    ModelResponse,
    ModelUsage,
    ToolSpec,
)


class DeepSeekConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    api_key: SecretStr
    model: str = "deepseek-v4-flash"
    base_url: str = "https://api.deepseek.com"
    thinking_enabled: bool = True
    reasoning_effort: str = "high"
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=16_384, ge=1, le=384_000)
    timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=10)
    cache_hit_cost_per_million: float = Field(default=0.0028, ge=0)
    cache_miss_cost_per_million: float = Field(default=0.14, ge=0)
    output_cost_per_million: float = Field(default=0.28, ge=0)

    @classmethod
    def from_env(cls) -> DeepSeekConfig:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is not set")
        return cls(api_key=SecretStr(api_key))


class ProviderRequestError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class JsonTransport(Protocol):
    def post(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class UrllibJsonTransport:
    def post(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ProviderRequestError(
                f"DeepSeek API returned HTTP {exc.code}: {body[:1_000]}",
                retryable=exc.code == 429 or exc.code >= 500,
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderRequestError(
                f"DeepSeek API connection failed: {exc.reason}",
                retryable=True,
            ) from exc
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise ProviderRequestError(
                "DeepSeek API returned a non-object response", retryable=False
            )
        return parsed


class DeepSeekProvider:
    def __init__(
        self,
        config: DeepSeekConfig,
        transport: JsonTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if config.model != "deepseek-v4-flash":
            raise ValueError("PatchLoop currently supports only deepseek-v4-flash")
        self.config = config
        self.transport = transport or UrllibJsonTransport()
        self.sleeper = sleeper

    @classmethod
    def from_env(cls) -> DeepSeekProvider:
        return cls(DeepSeekConfig.from_env())

    @property
    def name(self) -> str:
        return self.config.model

    def complete(
        self,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [self._message_payload(message) for message in messages],
            "tools": [self._tool_payload(tool) for tool in tools],
            "tool_choice": "auto",
            "thinking": {"type": "enabled" if self.config.thinking_enabled else "disabled"},
            "reasoning_effort": self.config.reasoning_effort,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }
        response = self._request_with_retry(payload)
        return self._parse_response(response)

    def _request_with_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = self.config.api_key.get_secret_value()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        endpoint = f"{self.config.base_url.rstrip('/')}/chat/completions"
        for attempt in range(self.config.max_retries + 1):
            try:
                return self.transport.post(
                    endpoint,
                    headers,
                    payload,
                    self.config.timeout_seconds,
                )
            except ProviderRequestError as exc:
                safe_message = str(exc).replace(api_key, "[REDACTED]")
                if not exc.retryable or attempt >= self.config.max_retries:
                    raise RuntimeError(safe_message) from exc
                self.sleeper(float(2**attempt))
        raise RuntimeError("DeepSeek request retry loop terminated unexpectedly")

    @staticmethod
    def _message_payload(message: ModelMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id
        return payload

    @staticmethod
    def _tool_payload(tool: ToolSpec) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": f"[{tool.permission}] {tool.description}",
                "parameters": tool.parameters,
            },
        }

    def _parse_response(self, response: dict[str, Any]) -> ModelResponse:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise RuntimeError("DeepSeek response does not contain a valid choice")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise RuntimeError("DeepSeek response does not contain a valid message")
        raw_content = message.get("content")
        content = raw_content if isinstance(raw_content, str) else ""
        calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls", [])
        if isinstance(raw_calls, list):
            for raw_call in raw_calls:
                if not isinstance(raw_call, dict):
                    continue
                function = raw_call.get("function")
                if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                    continue
                raw_arguments = function.get("arguments", "{}")
                arguments, arguments_error = DeepSeekProvider._parse_arguments(raw_arguments)
                raw_id = raw_call.get("id")
                calls.append(
                    ToolCall(
                        id=raw_id if isinstance(raw_id, str) else "missing-tool-call-id",
                        name=function["name"],
                        arguments=arguments,
                        arguments_error=arguments_error,
                    )
                )
        raw_usage = response.get("usage")
        usage = raw_usage if isinstance(raw_usage, dict) else {}
        input_tokens = self._integer(usage.get("prompt_tokens"))
        output_tokens = self._integer(usage.get("completion_tokens"))
        cache_hit_tokens = self._integer(usage.get("prompt_cache_hit_tokens"))
        cache_miss_tokens = self._integer(usage.get("prompt_cache_miss_tokens"))
        if cache_hit_tokens + cache_miss_tokens == 0:
            cache_miss_tokens = input_tokens
        estimated_cost = (
            cache_hit_tokens * self.config.cache_hit_cost_per_million
            + cache_miss_tokens * self.config.cache_miss_cost_per_million
            + output_tokens * self.config.output_cost_per_million
        ) / 1_000_000
        return ModelResponse(
            content=content,
            tool_calls=calls,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=estimated_cost,
            ),
        )

    @staticmethod
    def _parse_arguments(raw_arguments: Any) -> tuple[dict[str, Any], str | None]:
        if not isinstance(raw_arguments, str):
            return {}, "tool arguments are not a JSON string"
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            return {}, f"invalid tool arguments JSON: {exc.msg}"
        if not isinstance(parsed, dict):
            return {}, "tool arguments JSON must be an object"
        return parsed, None

    @staticmethod
    def _integer(value: Any) -> int:
        return value if isinstance(value, int) and value >= 0 else 0
