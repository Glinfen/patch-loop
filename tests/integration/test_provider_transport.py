from __future__ import annotations

import asyncio
import ssl

import httpx
import pytest
from pydantic import SecretStr

from patchloop.providers.base import EncodedRequest
from patchloop.providers.contracts import ProviderError, ProviderErrorKind, ProviderTransportConfig
from patchloop.providers.sse import SSEDecoder, SSEFrame
from patchloop.providers.transport import HttpxTransport, TransportControlError
from tests.support.provider_http_server import ProviderHTTPServer


def _request(path: str) -> EncodedRequest:
    return EncodedRequest(path=path, body={"hello": "world"})


def test_transport_streams_bounded_bytes_and_reuses_client_connections() -> None:
    with ProviderHTTPServer() as server:
        transport = HttpxTransport(server.base_url, credential=SecretStr("secret-value"))

        async def run() -> tuple[list[bytes], list[bytes], dict[str, str]]:
            async with transport.client_scope():
                first_headers: dict[str, str] = {}
                first: list[bytes] = []
                async with transport.open(_request("/json")) as response:
                    first_headers = dict(response.headers)
                    async for chunk in response.aiter_bytes():
                        first.append(chunk)
                second: list[bytes] = []
                async with transport.open(_request("/json")) as response:
                    async for chunk in response.aiter_bytes():
                        second.append(chunk)
                return first, second, first_headers

        first, second, headers = asyncio.run(run())

        assert b"".join(first) == b'{"ok":true}'
        assert b"".join(second) == b'{"ok":true}'
        assert headers["content-type"] == "application/json"
        assert headers["x-request-id"] == "test-request"
        assert "set-cookie" not in headers
        assert len({request["client_port"] for request in server.requests}) == 1
        assert server.requests[0]["headers"]["authorization"] == "Bearer secret-value"


def test_transport_does_not_follow_redirects_and_enforces_body_limit() -> None:
    with ProviderHTTPServer() as server:
        transport = HttpxTransport(
            server.base_url,
            config=ProviderTransportConfig(max_response_bytes=64),
        )

        async def check_redirect() -> int:
            async with transport.open(_request("/redirect")) as response:
                async for _ in response.aiter_bytes():
                    pass
                return response.status_code

        assert asyncio.run(check_redirect()) == 307
        assert [request["path"] for request in server.requests] == ["/redirect"]

        async def check_size() -> None:
            async with transport.open(_request("/large")) as response:
                async for _ in response.aiter_bytes():
                    pass

        with pytest.raises(ProviderError) as oversized:
            asyncio.run(check_size())
        assert oversized.value.kind is ProviderErrorKind.RESPONSE_TOO_LARGE


def test_transport_streams_sse_bytes_for_the_incremental_decoder() -> None:
    with ProviderHTTPServer() as server:
        transport = HttpxTransport(server.base_url)

        async def read() -> list[SSEFrame]:
            decoder = SSEDecoder()
            frames: list[SSEFrame] = []
            async with transport.open(_request("/sse")) as response:
                async for chunk in response.aiter_bytes():
                    frames.extend(decoder.feed(chunk))
                frames.extend(decoder.finish())
            return frames

        frames = asyncio.run(read())
        assert [frame.data for frame in frames] == ["中文🙂\nline two", "[DONE]"]
        assert [frame.id for frame in frames] == ["17", "17"]


@pytest.mark.parametrize(
    "path,started",
    [("/headers-block", "headers"), ("/stream-block", "stream")],
)
def test_control_cancellation_closes_connection_before_return(path: str, started: str) -> None:
    with ProviderHTTPServer() as server:
        action: list[str | None] = [None]

        def control() -> str | None:
            return action[0]

        transport = HttpxTransport(server.base_url)

        async def run() -> None:
            async with (
                transport.client_scope(),
                transport.open(
                    _request(path),
                    control=control,
                ) as response,
            ):
                async for _ in response.aiter_bytes():
                    pass

        async def exercise() -> None:
            task = asyncio.create_task(run())
            gate = server.headers_started if started == "headers" else server.stream_started
            reached = await asyncio.to_thread(gate.wait, 2)
            assert reached, "test server did not reach the requested cancellation point"
            action[0] = "cancel"
            with pytest.raises(TransportControlError):
                await asyncio.wait_for(task, timeout=2)
            if started == "headers":
                server.release_headers.set()
            closed = await asyncio.to_thread(server.disconnected.wait, 2)
            assert closed, "server did not observe the client connection closing"

        asyncio.run(exercise())


@pytest.mark.parametrize(
    ("path", "timeout_config", "started", "expected_kind"),
    [
        (
            "/headers-block",
            ProviderTransportConfig(
                connect_timeout_seconds=0.1,
                idle_timeout_seconds=2,
                total_timeout_seconds=0.2,
            ),
            "headers",
            ProviderErrorKind.TIMEOUT,
        ),
        (
            "/stream-block",
            ProviderTransportConfig(
                connect_timeout_seconds=0.1,
                idle_timeout_seconds=0.2,
                total_timeout_seconds=2,
            ),
            "stream",
            ProviderErrorKind.TIMEOUT,
        ),
    ],
)
def test_total_and_idle_timeouts_close_open_connections(
    path: str,
    timeout_config: ProviderTransportConfig,
    started: str,
    expected_kind: ProviderErrorKind,
) -> None:
    with ProviderHTTPServer() as server:
        transport = HttpxTransport(server.base_url, config=timeout_config)

        async def read() -> None:
            async with transport.open(_request(path)) as response:
                async for _ in response.aiter_bytes():
                    pass

        async def exercise() -> None:
            task = asyncio.create_task(read())
            gate = server.headers_started if started == "headers" else server.stream_started
            reached = await asyncio.to_thread(gate.wait, 2)
            assert reached, "test server did not reach the configured timeout point"
            with pytest.raises(ProviderError) as timeout_error:
                await asyncio.wait_for(task, timeout=2)
            assert timeout_error.value.kind is expected_kind
            if started == "headers":
                server.release_headers.set()
            closed = await asyncio.to_thread(server.disconnected.wait, 2)
            assert closed, "server did not observe the timed-out connection closing"

        asyncio.run(exercise())


def test_transport_settings_are_explicit_and_http_urls_are_restricted() -> None:
    client_settings: dict[str, object] = {}
    transport_settings: dict[str, object] = {}

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        client_settings.update(kwargs)
        return httpx.AsyncClient(**kwargs)  # type: ignore[arg-type]

    def transport_factory(**kwargs: object) -> httpx.AsyncBaseTransport:
        transport_settings.update(kwargs)
        return httpx.AsyncHTTPTransport(**kwargs)  # type: ignore[arg-type]

    transport = HttpxTransport(
        "https://example.invalid/v1",
        config=ProviderTransportConfig(
            proxy_url="http://127.0.0.1:8080",
            ca_bundle=ssl.get_default_verify_paths().cafile,
        ),
        client_factory=client_factory,
        transport_factory=transport_factory,
    )

    async def open_scope() -> None:
        async with transport.client_scope():
            pass

    asyncio.run(open_scope())
    assert client_settings["trust_env"] is False
    assert client_settings["follow_redirects"] is False
    assert transport_settings["trust_env"] is False
    assert transport_settings["proxy"] == "http://127.0.0.1:8080"
    assert transport_settings["retries"] == 0
    verify_context = transport_settings["verify"]
    assert isinstance(verify_context, ssl.SSLContext)
    assert verify_context.check_hostname
    assert verify_context.verify_mode == ssl.CERT_REQUIRED

    with pytest.raises(ProviderError) as public_http:
        HttpxTransport("http://provider.example/v1")
    assert public_http.value.kind is ProviderErrorKind.CONFIGURATION
    with pytest.raises(ProviderError):
        HttpxTransport("https://user:password@example.invalid/v1")


def test_transport_reports_truncated_response_without_exposing_http_details() -> None:
    with ProviderHTTPServer() as server:
        transport = HttpxTransport(server.base_url)

        async def read() -> None:
            async with transport.open(_request("/truncated")) as response:
                async for _ in response.aiter_bytes():
                    pass

        with pytest.raises(ProviderError) as truncated:
            asyncio.run(read())
        assert truncated.value.kind is ProviderErrorKind.TRUNCATED
        assert "private" not in truncated.value.safe_message
