"""Controllable loopback HTTP server used by provider transport tests."""

from __future__ import annotations

import json
import select
import socket
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class ScriptedHTTPResponse:
    status: int
    body: bytes
    content_type: str = "application/json"


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], owner: ProviderHTTPServer) -> None:
        self.owner = owner
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def owner(self) -> ProviderHTTPServer:
        return self.server.owner  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.owner.record_request(
            self.path,
            self.client_address[1],
            dict(self.headers.items()),
            body,
        )

        if scripted := self.owner.take_response(self.path):
            self._write_response(scripted.status, scripted.body, scripted.content_type)
            return

        if self.path == "/redirect":
            self.send_response(307)
            self.send_header("Location", "/json")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/headers-block":
            self.owner.headers_started.set()
            self.owner.release_headers.wait(5)
            if not self._write_response(200, b'{"late":true}', "application/json"):
                self.owner.disconnected.set()
                return
            self._observe_disconnect()
            return
        if self.path == "/stream-block":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"f\r\ndata: partial\n\n\r\n")
            self.wfile.flush()
            self.owner.stream_started.set()
            self._observe_disconnect()
            return
        if self.path == "/close":
            self.close_connection = True
            return
        if self.path == "/truncated":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"short")
            self.wfile.flush()
            self.close_connection = True
            return
        if self.path == "/large":
            body = b"x" * 2048
            self._write_response(200, body, "application/octet-stream")
            return
        if self.path == "/sse":
            body = ("data: 中文🙂\r\nid: 17\r\ndata: line two\r\n\r\ndata: [DONE]\n\n").encode()
            self._write_response(200, body, "text/event-stream")
            return

        self._write_response(200, b'{"ok":true}', "application/json")

    def _write_response(self, status: int, body: bytes, content_type: str) -> bool:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "test=private")
            self.send_header("X-Request-ID", "test-request")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _observe_disconnect(self) -> None:
        connection: socket.socket = self.connection
        connection.setblocking(False)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            readable, _, _ = select.select([connection], [], [], 0.05)
            if not readable:
                continue
            try:
                data = connection.recv(1, socket.MSG_PEEK)
            except (BlockingIOError, ConnectionResetError, OSError):
                self.owner.disconnected.set()
                return
            if data == b"":
                self.owner.disconnected.set()
                return
            # There should be no further request bytes; consume unexpected data to
            # keep the monitor from spinning on a readable socket.
            try:
                connection.recv(4096)
            except OSError:
                self.owner.disconnected.set()
                return


class ProviderHTTPServer:
    """Threaded loopback server with deterministic gates for cancel tests."""

    def __init__(self) -> None:
        self.headers_started = threading.Event()
        self.release_headers = threading.Event()
        self.stream_started = threading.Event()
        self.disconnected = threading.Event()
        self._lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self._responses: defaultdict[str, deque[ScriptedHTTPResponse]] = defaultdict(deque)
        self._server = _Server(("127.0.0.1", 0), self)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> ProviderHTTPServer:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.release_headers.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def enqueue_json(self, path: str, body: dict[str, object], *, status: int = 200) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        with self._lock:
            self._responses[path].append(ScriptedHTTPResponse(status=status, body=encoded))

    def take_response(self, path: str) -> ScriptedHTTPResponse | None:
        with self._lock:
            queue = self._responses[path]
            return queue.popleft() if queue else None

    def record_request(
        self,
        path: str,
        client_port: int,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        with self._lock:
            self.requests.append(
                {
                    "path": path,
                    "client_port": client_port,
                    "headers": headers,
                    "body": body,
                }
            )

    def wait_for(self, event: threading.Event, timeout: float = 2) -> bool:
        return event.wait(timeout)
