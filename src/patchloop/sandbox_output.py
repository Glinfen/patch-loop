"""Bounded, streaming stdout/stderr collection for sandboxed processes."""

from __future__ import annotations

import codecs
import subprocess
import threading
from collections.abc import Callable
from time import monotonic
from typing import BinaryIO

from pydantic import BaseModel, Field


class CapturedOutput(BaseModel):
    stdout_tail: str = ""
    stderr_tail: str = ""
    stdout_bytes: int = Field(default=0, ge=0)
    stderr_bytes: int = Field(default=0, ge=0)
    truncated: bool = False


class OutputCollectionError(RuntimeError):
    pass


class OutputCollectionTimeout(OutputCollectionError):
    pass


class OutputCollectionInterrupted(OutputCollectionError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _TailBuffer:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.tail = ""
        self.byte_count = 0
        self.char_count = 0
        self.lock = threading.Lock()

    def append(self, raw: bytes, decoded: str) -> None:
        with self.lock:
            self.byte_count += len(raw)
            self.char_count += len(decoded)
            if decoded:
                self.tail = (self.tail + decoded)[-self.limit :]

    def finish(self, decoded: str) -> None:
        with self.lock:
            self.char_count += len(decoded)
            if decoded:
                self.tail = (self.tail + decoded)[-self.limit :]

    def snapshot(self) -> tuple[str, int, int]:
        with self.lock:
            return self.tail, self.byte_count, self.char_count


def _read_stream(
    stream: BinaryIO,
    buffer: _TailBuffer,
    errors: list[BaseException],
) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while True:
            raw = stream.read(16 * 1024)
            if not raw:
                break
            buffer.append(raw, decoder.decode(raw, final=False))
        buffer.finish(decoder.decode(b"", final=True))
    except BaseException as exc:
        errors.append(exc)


def collect_process_output(
    process: subprocess.Popen[bytes],
    *,
    max_output_chars: int,
    deadline: float,
    interruption_probe: Callable[[], str | None] | None,
    terminate: Callable[[str], None],
    readers_started: threading.Event | None = None,
) -> CapturedOutput:
    """Drain both pipes with O(max_output_chars) retained memory.

    ``terminate`` must stop the whole managed process domain and wait for it.  It is
    invoked before readers are joined so descendants cannot keep inherited pipe
    handles open indefinitely.
    """

    if max_output_chars <= 0:
        raise ValueError("max_output_chars must be positive")
    if process.stdout is None or process.stderr is None:
        raise ValueError("stdout and stderr must both be piped")

    stdout_buffer = _TailBuffer(max_output_chars)
    stderr_buffer = _TailBuffer(max_output_chars)
    reader_errors: list[BaseException] = []
    readers = [
        threading.Thread(
            target=_read_stream,
            args=(process.stdout, stdout_buffer, reader_errors),
            name=f"sandbox-stdout-{process.pid}",
        ),
        threading.Thread(
            target=_read_stream,
            args=(process.stderr, stderr_buffer, reader_errors),
            name=f"sandbox-stderr-{process.pid}",
        ),
    ]
    for reader in readers:
        reader.start()
    if readers_started is not None:
        readers_started.set()

    pending_error: OutputCollectionError | None = None
    termination_error: BaseException | None = None
    try:
        while process.poll() is None:
            if monotonic() >= deadline:
                try:
                    terminate("timeout")
                except BaseException as exc:
                    termination_error = exc
                pending_error = OutputCollectionTimeout("process output collection timed out")
                break
            reason = None if interruption_probe is None else interruption_probe()
            if reason is not None:
                try:
                    terminate(reason)
                except BaseException as exc:
                    termination_error = exc
                pending_error = OutputCollectionInterrupted(reason)
                break
            threading.Event().wait(0.1)

        join_deadline = monotonic() + 2
        for reader in readers:
            reader.join(max(0.0, join_deadline - monotonic()))
        if any(reader.is_alive() for reader in readers):
            process.stdout.close()
            process.stderr.close()
            for reader in readers:
                reader.join(0.2)
            raise OutputCollectionError("process output pipes did not reach EOF after cleanup")
        if reader_errors:
            raise OutputCollectionError(
                "could not read managed process output"
            ) from reader_errors[0]
        if termination_error is not None:
            raise termination_error
        if pending_error is not None:
            raise pending_error

        stdout_tail, stdout_bytes, stdout_chars = stdout_buffer.snapshot()
        stderr_tail, stderr_bytes, stderr_chars = stderr_buffer.snapshot()
        return CapturedOutput(
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            truncated=(stdout_chars + stderr_chars) > max_output_chars,
        )
    finally:
        if not any(reader.is_alive() for reader in readers):
            process.stdout.close()
            process.stderr.close()
