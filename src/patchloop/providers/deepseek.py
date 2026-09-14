"""DeepSeek provider compatibility facade backed by the provider gateway."""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from patchloop.providers.base import (
    EncodedRequest,
    ModelMessage,
    ModelResponse,
    ProviderControl,
    ToolSpec,
)
from patchloop.providers.chat import ChatCompletionsAdapter
from patchloop.providers.contracts import (
    ChatDialect,
    ProviderAuth,
    ProviderBinding,
    ProviderCapabilities,
    ProviderError,
    ProviderErrorKind,
    ProviderGeneration,
    ProviderPricing,
    ProviderProtocol,
    ProviderTransportConfig,
    ReasoningTransport,
)
from patchloop.providers.gateway import ProviderGateway
from patchloop.providers.transport import (
    AsyncTransport,
    HttpxTransport,
    TransportControlError,
    TransportResponse,
)

DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"


class DeepSeekConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    api_key: SecretStr
    model: str = DEFAULT_DEEPSEEK_MODEL
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
    def from_env(cls, env_file: Path | None = None) -> DeepSeekConfig:
        file_values = _load_env_file(env_file) if env_file is not None else {}
        api_key = _configuration_value(
            file_values,
            "DEEPSEEK_API_KEY",
            "LLM_API_KEY",
        )
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY or LLM_API_KEY is not set")
        base_url = _configuration_value(
            file_values,
            "DEEPSEEK_BASE_URL",
            "LLM_BASE_URL",
        )
        model = _configuration_value(
            file_values,
            "DEEPSEEK_MODEL",
            "LLM_MODEL_ID",
        )
        return cls(
            api_key=SecretStr(api_key),
            base_url=base_url or "https://api.deepseek.com",
            model=model or DEFAULT_DEEPSEEK_MODEL,
        )


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if not separator or not name.isidentifier():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return values


def _configuration_value(file_values: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    for name in names:
        value = file_values.get(name)
        if value:
            return value
    return None


class ProviderRequestError(RuntimeError):
    """Compatibility exception raised by the legacy synchronous transport."""

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
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            raise ProviderRequestError(
                "DeepSeek API returned invalid JSON", retryable=False
            ) from None
        if not isinstance(parsed, dict):
            raise ProviderRequestError(
                "DeepSeek API returned a non-object response", retryable=False
            )
        return parsed


class _LegacyJsonTransportBridge:
    """Adapt the old blocking JSON injection seam to the async gateway transport."""

    def __init__(
        self,
        base_url: str,
        api_key: SecretStr,
        transport: JsonTransport,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport

    @asynccontextmanager
    async def client_scope(self) -> AsyncIterator[None]:
        yield

    @asynccontextmanager
    async def open(
        self,
        encoded: EncodedRequest,
        timeouts: ProviderTransportConfig,
        *,
        control: ProviderControl | None = None,
    ) -> AsyncIterator[TransportResponse]:
        if control is not None:
            try:
                action = control()
            except Exception:
                raise ProviderError(
                    ProviderErrorKind.OBSERVER,
                    "provider execution control callback failed",
                ) from None
            if action is not None:
                raise TransportControlError(action)
        headers = dict(encoded.headers)
        headers["Authorization"] = f"Bearer {self.api_key.get_secret_value()}"
        endpoint = f"{self.base_url}/{encoded.path.lstrip('/')}"
        try:
            payload = await asyncio.to_thread(
                self.transport.post,
                endpoint,
                headers,
                encoded.body,
                timeouts.total_timeout_seconds,
            )
        except ProviderRequestError as exc:
            safe_message = str(exc).replace(self.api_key.get_secret_value(), "[REDACTED]")
            raise ProviderError(
                ProviderErrorKind.CONNECTION,
                safe_message,
                request_sent=False,
                retryable=exc.retryable,
                usage_unknown=True,
            ) from None
        except Exception:
            raise ProviderError(
                ProviderErrorKind.CONNECTION,
                "legacy DeepSeek transport failed",
                request_sent=False,
                usage_unknown=True,
            ) from None

        try:
            response_bytes = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise ProviderError(
                ProviderErrorKind.PROTOCOL,
                "legacy DeepSeek transport returned an invalid response",
                request_sent=True,
                usage_unknown=True,
            ) from None
        response = httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=response_bytes,
        )
        wrapped = TransportResponse(
            response,
            max_response_bytes=timeouts.max_response_bytes,
            control=None,
            deadline=time.monotonic() + timeouts.total_timeout_seconds,
            clock=time.monotonic,
        )
        try:
            yield wrapped
        finally:
            await wrapped.aclose()


class DeepSeekProvider:
    """Keep the established provider facade while using PGW lifecycle handling."""

    def __init__(
        self,
        config: DeepSeekConfig,
        transport: JsonTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibJsonTransport()
        self.sleeper = sleeper
        binding = self._binding(config, legacy=transport is not None)
        adapter = ChatCompletionsAdapter(ChatDialect.DEEPSEEK)
        if transport is None:
            gateway_transport: AsyncTransport = HttpxTransport(
                config.base_url,
                credential=config.api_key,
                config=binding.transport,
            )
            self.supports_live_cancellation = True
            if sleeper is time.sleep:
                self.gateway = ProviderGateway(binding, adapter, gateway_transport)
            else:

                async def injected_sleep(delay: float) -> None:
                    await asyncio.to_thread(sleeper, delay)

                self.gateway = ProviderGateway(
                    binding,
                    adapter,
                    gateway_transport,
                    random_source=lambda: 0.0,
                    sleep=injected_sleep,
                )
        else:
            gateway_transport = _LegacyJsonTransportBridge(
                config.base_url,
                config.api_key,
                transport,
            )

            async def legacy_sleep(delay: float) -> None:
                await asyncio.to_thread(sleeper, delay)

            self.supports_live_cancellation = False
            self.gateway = ProviderGateway(
                binding,
                adapter,
                gateway_transport,
                random_source=lambda: 0.0,
                sleep=legacy_sleep,
            )

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> DeepSeekProvider:
        return cls(DeepSeekConfig.from_env(env_file))

    @property
    def name(self) -> str:
        return self.config.model

    def complete(
        self,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        return self.gateway.complete(messages, tools)

    @staticmethod
    def _binding(config: DeepSeekConfig, *, legacy: bool) -> ProviderBinding:
        capabilities = ProviderCapabilities(
            tools=True,
            multiple_tool_calls=True,
            streaming=True,
            reasoning_transport=ReasoningTransport.DEEPSEEK_TEXT,
            structured_output=False,
            context_window_tokens=max(32_000, config.max_tokens + 8_192),
            max_output_tokens=config.max_tokens,
            usage_supported=True,
            cache_usage_supported=True,
        )
        timeout = config.timeout_seconds
        transport = ProviderTransportConfig(
            streaming=not legacy,
            connect_timeout_seconds=min(10.0, timeout),
            write_timeout_seconds=min(30.0, timeout),
            idle_timeout_seconds=timeout,
            total_timeout_seconds=timeout,
            max_retries=config.max_retries,
        )
        return ProviderBinding(
            profile_id="deepseek",
            protocol=ProviderProtocol.CHAT_COMPLETIONS,
            dialect=ChatDialect.DEEPSEEK,
            model=config.model,
            base_url=config.base_url,
            auth=ProviderAuth.BEARER,
            credential_env="DEEPSEEK_API_KEY",
            capabilities=capabilities,
            generation=ProviderGeneration(
                max_output_tokens=config.max_tokens,
                temperature=config.temperature,
                reasoning_enabled=config.thinking_enabled,
                reasoning_effort=config.reasoning_effort if config.thinking_enabled else None,
            ),
            transport=transport,
            pricing=ProviderPricing(
                version="deepseek-configured",
                input_per_million=config.cache_miss_cost_per_million,
                output_per_million=config.output_cost_per_million,
                cached_input_per_million=config.cache_hit_cost_per_million,
            ),
        )


__all__ = [
    "DEFAULT_DEEPSEEK_MODEL",
    "DeepSeekConfig",
    "DeepSeekProvider",
    "JsonTransport",
    "ProviderRequestError",
    "UrllibJsonTransport",
]
