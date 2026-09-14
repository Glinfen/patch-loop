"""Bounded, cancellable HTTP transport for provider adapters."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from patchloop.providers.base import EncodedRequest, ProviderControl
from patchloop.providers.contracts import (
    ProviderError,
    ProviderErrorKind,
    ProviderTransportConfig,
)

type Clock = Callable[[], float]
_POLL_INTERVAL_SECONDS = 0.1
_STREAM_CHUNK_BYTES = 64 * 1024
_SAFE_RESPONSE_HEADERS = frozenset(
    {"content-length", "content-type", "retry-after", "x-request-id", "openai-request-id"}
)


class TransportControlError(ProviderError):
    """A provider request stopped by the caller's runtime control callback."""

    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(
            ProviderErrorKind.CANCELLED,
            "provider request interrupted by execution control",
            request_sent=True,
            usage_unknown=True,
        )


class AsyncTransport(Protocol):
    """Structural interface used by protocol-independent gateway code."""

    def client_scope(self) -> AbstractAsyncContextManager[None]: ...

    def open(
        self,
        encoded: EncodedRequest,
        timeouts: ProviderTransportConfig,
        *,
        control: ProviderControl | None = None,
    ) -> AbstractAsyncContextManager[TransportResponse]: ...


class TransportResponse:
    """A streaming response exposing only status and a small header allowlist."""

    def __init__(
        self,
        response: httpx.Response,
        *,
        max_response_bytes: int,
        control: ProviderControl | None,
        deadline: float,
        clock: Clock,
    ) -> None:
        self.status_code = response.status_code
        safe_headers = {
            name.lower(): value
            for name, value in response.headers.items()
            if name.lower() in _SAFE_RESPONSE_HEADERS
        }
        self.headers: Mapping[str, str] = MappingProxyType(safe_headers)
        self._response = response
        self._max_response_bytes = max_response_bytes
        self._control = control
        self._deadline = deadline
        self._clock = clock
        self._bytes_read = 0
        self._closed = False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        iterator = self._response.aiter_bytes(chunk_size=_STREAM_CHUNK_BYTES).__aiter__()
        while True:
            try:
                chunk = await _await_supervised(
                    iterator.__anext__(),
                    control=self._control,
                    deadline=self._deadline,
                    clock=self._clock,
                    request_sent=True,
                    response=self._response,
                )
            except StopAsyncIteration:
                return
            except ProviderError:
                raise
            except httpx.HTTPError as exc:
                raise _provider_error_for_httpx(exc, request_sent=True) from None

            self._bytes_read += len(chunk)
            if self._bytes_read > self._max_response_bytes:
                raise ProviderError(
                    ProviderErrorKind.RESPONSE_TOO_LARGE,
                    "provider response exceeded the configured size limit",
                    request_sent=True,
                    partial_output=self._bytes_read > 0,
                    usage_unknown=True,
                )
            if chunk:
                yield chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._response.aclose()
        except Exception:
            raise ProviderError(
                ProviderErrorKind.TRANSPORT_CLEANUP_FAILED,
                "provider response could not be closed cleanly",
                request_sent=True,
                usage_unknown=True,
            ) from None


class HttpxTransport:
    """HTTPX transport with explicit trust, timeout, cancellation, and size limits.

    Use ``client_scope`` around a logical request when it has multiple attempts;
    every ``open`` inside that scope reuses one AsyncClient. Standalone ``open``
    calls create and close their own client.
    """

    def __init__(
        self,
        base_url: str,
        *,
        credential: SecretStr | None = None,
        config: ProviderTransportConfig | None = None,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
        transport_factory: Callable[..., httpx.AsyncBaseTransport] = httpx.AsyncHTTPTransport,
        clock: Clock = time.monotonic,
    ) -> None:
        self.base_url = _validated_base_url(base_url)
        self.credential = credential
        self.config = config or ProviderTransportConfig()
        self._client_factory = client_factory
        self._transport_factory = transport_factory
        self._clock = clock
        self._client: httpx.AsyncClient | None = None
        self._scope_task: asyncio.Task[Any] | None = None

    @asynccontextmanager
    async def client_scope(self) -> AsyncIterator[None]:
        """Create one client to be reused for all attempts in a request lifecycle."""

        if self._client is not None:
            raise RuntimeError("an HTTP client scope is already active")

        verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=self.config.ca_bundle)
            if self.config.ca_bundle
            else True
        )
        network_transport = self._transport_factory(
            retries=0,
            verify=verify,
            proxy=self.config.proxy_url,
            trust_env=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        client = self._client_factory(
            timeout=_httpx_timeout(self.config),
            transport=network_transport,
            trust_env=False,
            follow_redirects=False,
        )
        self._client = client
        self._scope_task = asyncio.current_task()
        try:
            yield
        finally:
            self._client = None
            self._scope_task = None
            try:
                await client.aclose()
            except Exception:
                raise ProviderError(
                    ProviderErrorKind.TRANSPORT_CLEANUP_FAILED,
                    "provider HTTP client could not be closed cleanly",
                    request_sent=True,
                    usage_unknown=True,
                ) from None

    @asynccontextmanager
    async def open(
        self,
        encoded: EncodedRequest,
        timeouts: ProviderTransportConfig | None = None,
        *,
        control: ProviderControl | None = None,
    ) -> AsyncIterator[TransportResponse]:
        """Open a streaming HTTP response without following redirects."""

        if self._client is None:
            async with (
                self.client_scope(),
                self._open_in_scope(
                    encoded,
                    timeouts,
                    control=control,
                ) as response,
            ):
                yield response
            return
        if self._scope_task is not asyncio.current_task():
            raise RuntimeError("HTTP transport scope must be used by its owning task")
        async with self._open_in_scope(encoded, timeouts, control=control) as response:
            yield response

    @asynccontextmanager
    async def _open_in_scope(
        self,
        encoded: EncodedRequest,
        timeouts: ProviderTransportConfig | None,
        *,
        control: ProviderControl | None,
    ) -> AsyncIterator[TransportResponse]:
        assert self._client is not None
        request_config = timeouts or self.config
        deadline = self._clock() + request_config.total_timeout_seconds
        response: httpx.Response | None = None
        wrapped: TransportResponse | None = None
        request = self._build_request(encoded, request_config)
        try:
            response = await _await_supervised(
                self._client.send(request, stream=True, follow_redirects=False),
                control=control,
                deadline=deadline,
                clock=self._clock,
                request_sent=True,
            )
            wrapped = TransportResponse(
                response,
                max_response_bytes=request_config.max_response_bytes,
                control=control,
                deadline=deadline,
                clock=self._clock,
            )
            yield wrapped
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise _provider_error_for_httpx(exc, request_sent=False) from None
        except asyncio.CancelledError:
            raise
        finally:
            if wrapped is not None:
                await wrapped.aclose()
            elif response is not None:
                try:
                    await response.aclose()
                except Exception:
                    raise ProviderError(
                        ProviderErrorKind.TRANSPORT_CLEANUP_FAILED,
                        "provider response could not be closed cleanly",
                        request_sent=True,
                        usage_unknown=True,
                    ) from None

    def _build_request(
        self,
        encoded: EncodedRequest,
        config: ProviderTransportConfig,
    ) -> httpx.Request:
        if self._client is None:
            raise RuntimeError("HTTP client scope is not active")
        path = encoded.path
        parsed_path = urlsplit(path)
        if (
            parsed_path.scheme
            or parsed_path.netloc
            or parsed_path.query
            or parsed_path.fragment
            or "?" in path
            or "#" in path
        ):
            raise ProviderError(
                ProviderErrorKind.CONFIGURATION,
                "provider request path must be a relative endpoint without a query",
            )
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

        headers: dict[str, str] = {}
        for name, value in encoded.headers.items():
            if name.lower() == "authorization":
                raise ProviderError(
                    ProviderErrorKind.CONFIGURATION,
                    "authorization must be supplied through the configured credential",
                )
            headers[name] = value
        headers.setdefault("content-type", "application/json")
        if self.credential is not None:
            token = self.credential.get_secret_value()
            headers["authorization"] = f"Bearer {token}"

        try:
            body = json.dumps(
                encoded.body,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise ProviderError(
                ProviderErrorKind.CONFIGURATION,
                "provider request body is not valid JSON",
            ) from None
        return self._client.build_request(
            encoded.method,
            url,
            headers=headers,
            content=body,
            timeout=_httpx_timeout(config),
        )


async def _await_supervised[T](
    awaitable: Awaitable[T],
    *,
    control: ProviderControl | None,
    deadline: float,
    clock: Callable[[], float],
    request_sent: bool,
    response: httpx.Response | None = None,
) -> T:
    """Wait in bounded intervals so execution controls and deadlines stay live."""

    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            if control is not None:
                action = control()
                if action is not None:
                    await _cancel_and_wait(task)
                    raise TransportControlError(action)

            remaining = deadline - clock()
            if remaining <= 0:
                await _cancel_and_wait(task)
                raise ProviderError(
                    ProviderErrorKind.TIMEOUT,
                    "provider request exceeded its total time limit",
                    request_sent=request_sent,
                    partial_output=response is not None,
                    retryable=False,
                    usage_unknown=request_sent,
                )

            wait_seconds = (
                min(remaining, _POLL_INTERVAL_SECONDS) if control is not None else remaining
            )
            done, _ = await asyncio.wait({task}, timeout=wait_seconds)
            if done:
                return task.result()
    except asyncio.CancelledError:
        await _cancel_and_wait(task)
        raise
    except BaseException:
        await _cancel_and_wait(task)
        raise


async def _cancel_and_wait(task: asyncio.Future[Any]) -> None:
    if not task.done():
        task.cancel()
    with suppress(BaseException):
        await task


def _httpx_timeout(config: ProviderTransportConfig) -> httpx.Timeout:
    return httpx.Timeout(
        connect=config.connect_timeout_seconds,
        write=config.write_timeout_seconds,
        read=config.idle_timeout_seconds,
        pool=config.connect_timeout_seconds,
    )


def _provider_error_for_httpx(exc: httpx.HTTPError, *, request_sent: bool) -> ProviderError:
    if isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout)):
        return ProviderError(
            ProviderErrorKind.TIMEOUT,
            "provider connection timed out",
            request_sent=False,
            retryable=True,
        )
    if isinstance(exc, httpx.ConnectError):
        return ProviderError(
            ProviderErrorKind.CONNECTION,
            "provider connection could not be established",
            request_sent=False,
            retryable=True,
        )
    if isinstance(exc, (httpx.WriteTimeout, httpx.WriteError)):
        kind = (
            ProviderErrorKind.TIMEOUT
            if isinstance(exc, httpx.WriteTimeout)
            else ProviderErrorKind.CONNECTION
        )
        return ProviderError(
            kind,
            "provider request could not be sent completely",
            request_sent=True,
            usage_unknown=True,
        )
    if isinstance(exc, (httpx.ReadTimeout, httpx.ReadError)):
        kind = (
            ProviderErrorKind.TIMEOUT
            if isinstance(exc, httpx.ReadTimeout)
            else ProviderErrorKind.TRUNCATED
        )
        return ProviderError(
            kind,
            "provider response stream ended before it was complete",
            request_sent=True,
            partial_output=True,
            usage_unknown=True,
        )
    if isinstance(exc, httpx.RemoteProtocolError):
        return ProviderError(
            ProviderErrorKind.TRUNCATED,
            "provider closed the response before it was complete",
            request_sent=True,
            partial_output=True,
            usage_unknown=True,
        )
    if isinstance(exc, httpx.UnsupportedProtocol):
        return ProviderError(
            ProviderErrorKind.CONFIGURATION,
            "provider URL uses an unsupported transport protocol",
        )
    return ProviderError(
        ProviderErrorKind.CONNECTION,
        "provider transport failed",
        request_sent=request_sent,
        usage_unknown=request_sent,
    )


def _validated_base_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderError(
            ProviderErrorKind.CONFIGURATION,
            "provider base URL is not a safe HTTP endpoint",
        )
    if parsed.scheme == "http":
        host = parsed.hostname.lower().rstrip(".")
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host == "localhost"
        if not is_loopback:
            raise ProviderError(
                ProviderErrorKind.CONFIGURATION,
                "unencrypted provider HTTP is allowed only for loopback endpoints",
            )
    return base_url.rstrip("/")
