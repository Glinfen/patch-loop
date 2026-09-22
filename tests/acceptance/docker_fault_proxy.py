"""Run-scoped Unix socket proxy used for real Docker transport fault injection."""

from __future__ import annotations

import os
import socket
import stat
import threading
from contextlib import suppress
from pathlib import Path


class DockerFaultProxy:
    """Forward a private Unix socket to a local Docker socket without logging data."""

    def __init__(self, upstream_socket: Path | str, control_dir: Path) -> None:
        self.upstream_socket = Path(upstream_socket).resolve(strict=True)
        self.control_dir = control_dir.resolve()
        self.socket_path = self.control_dir / "docker-proxy.sock"
        self._listener: socket.socket | None = None
        self._listener_thread: threading.Thread | None = None
        self._connections: set[socket.socket] = set()
        self._workers: set[threading.Thread] = set()
        self._lock = threading.RLock()
        self._closing = False
        self._socket_identity: tuple[int, int] | None = None
        self._cut_after_accepted: int | None = None
        self._accepted_connections = 0

    def start(self) -> str:
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("Unix sockets are unavailable on this platform")
        upstream = self.upstream_socket.stat()
        if not stat.S_ISSOCK(upstream.st_mode):
            raise ValueError("upstream_socket must be a Unix socket")
        self.control_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise FileExistsError(self.socket_path)
        self._closing = False
        self._open_listener()
        return f"unix://{self.socket_path}"

    def cut_connections(self) -> None:
        with self._lock:
            listener = self._listener
            self._listener = None
            connections = list(self._connections)
        if listener is not None:
            listener.close()
        for connection in connections:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            connection.close()
        thread = self._listener_thread
        if thread is not None:
            thread.join(timeout=2)
        self._unlink_owned_socket()

    def restore(self) -> None:
        with self._lock:
            if self._closing:
                raise RuntimeError("proxy is closed")
            if self._listener is not None:
                return
        self._open_listener()

    def cut_after_connections(self, count: int = 1) -> None:
        """Allow *count* new connections, then cut the listener for later clients."""

        if count <= 0:
            raise ValueError("count must be positive")
        with self._lock:
            if self._listener is None:
                raise RuntimeError("proxy listener is not active")
            self._cut_after_accepted = self._accepted_connections + count

    def close(self) -> None:
        with self._lock:
            self._closing = True
        self.cut_connections()
        for worker in list(self._workers):
            worker.join(timeout=2)
        self._unlink_owned_socket()
        with suppress(OSError):
            self.control_dir.rmdir()

    def _open_listener(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            listener.listen(16)
            listener.settimeout(0.2)
            identity = self.socket_path.stat()
        except BaseException:
            listener.close()
            raise
        with self._lock:
            self._listener = listener
            self._socket_identity = (identity.st_dev, identity.st_ino)
        thread = threading.Thread(target=self._accept, name="docker-fault-proxy", daemon=True)
        self._listener_thread = thread
        thread.start()

    def _accept(self) -> None:
        while True:
            with self._lock:
                listener = self._listener
                closing = self._closing
            if listener is None or closing:
                return
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._lock:
                self._accepted_connections += 1
                return_after_bridge = (
                    self._cut_after_accepted is not None
                    and self._accepted_connections >= self._cut_after_accepted
                )
                if return_after_bridge:
                    self._cut_after_accepted = None
                    self._listener = None
            if return_after_bridge:
                listener.close()
                self._unlink_owned_socket()
            try:
                upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                upstream.connect(str(self.upstream_socket))
            except OSError:
                client.close()
                continue
            with self._lock:
                self._connections.update({client, upstream})
            worker = threading.Thread(
                target=self._bridge,
                args=(client, upstream),
                name="docker-fault-connection",
                daemon=True,
            )
            with self._lock:
                self._workers.add(worker)
            worker.start()
            if return_after_bridge:
                return

    def _bridge(self, client: socket.socket, upstream: socket.socket) -> None:
        pumps = [
            threading.Thread(target=self._pump, args=(client, upstream), daemon=True),
            threading.Thread(target=self._pump, args=(upstream, client), daemon=True),
        ]
        for pump in pumps:
            pump.start()
        for pump in pumps:
            pump.join()
        for connection in (client, upstream):
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            connection.close()
        with self._lock:
            self._connections.discard(client)
            self._connections.discard(upstream)
            self._workers.discard(threading.current_thread())

    @staticmethod
    def _pump(source: socket.socket, destination: socket.socket) -> None:
        try:
            while chunk := source.recv(64 * 1024):
                destination.sendall(chunk)
        except OSError:
            pass
        with suppress(OSError):
            destination.shutdown(socket.SHUT_WR)

    def _unlink_owned_socket(self) -> None:
        identity = self._socket_identity
        if identity is None:
            return
        try:
            current = self.socket_path.lstat()
        except FileNotFoundError:
            self._socket_identity = None
            return
        if not stat.S_ISSOCK(current.st_mode):
            raise RuntimeError("proxy socket path was replaced with a non-socket")
        if (current.st_dev, current.st_ino) != identity:
            raise RuntimeError("proxy socket identity changed")
        self.socket_path.unlink()
        self._socket_identity = None


__all__ = ["DockerFaultProxy"]
