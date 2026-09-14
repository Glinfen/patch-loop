"""Small, bounded Server-Sent Events decoder for provider streams."""

from __future__ import annotations

from dataclasses import dataclass

from patchloop.providers.contracts import ProviderError, ProviderErrorKind


@dataclass(frozen=True, slots=True)
class SSEFrame:
    event: str | None
    data: str
    id: str | None


class SSEDecoder:
    """Decode SSE bytes incrementally without assuming transport chunk boundaries.

    A blank line dispatches a frame. ``finish`` also dispatches a final pending
    frame so protocol reducers can inspect it; it does not imply that a streamed
    model response completed successfully.
    """

    def __init__(self, *, max_frame_bytes: int = 1_048_576) -> None:
        if max_frame_bytes <= 0:
            raise ValueError("max_frame_bytes must be positive")
        self.max_frame_bytes = max_frame_bytes
        self._line_buffer = bytearray()
        self._frame_bytes = 0
        self._data_lines: list[str] = []
        self._has_data = False
        self._event: str | None = None
        self._last_event_id: str | None = None
        self._at_stream_start = True
        self._finished = False

    def feed(self, chunk: bytes) -> list[SSEFrame]:
        if self._finished:
            raise ValueError("cannot feed an SSE decoder after finish")
        if not isinstance(chunk, bytes):
            raise TypeError("SSEDecoder.feed expects bytes")

        frames: list[SSEFrame] = []
        offset = 0
        while offset < len(chunk):
            newline = chunk.find(b"\n", offset)
            if newline < 0:
                self._line_buffer.extend(chunk[offset:])
                self._ensure_frame_size(len(self._line_buffer))
                break

            self._line_buffer.extend(chunk[offset:newline])
            self._ensure_frame_size(len(self._line_buffer) + 1)
            frames.extend(self._process_line(bytes(self._line_buffer)))
            self._line_buffer.clear()
            offset = newline + 1

        return frames

    def finish(self) -> list[SSEFrame]:
        if self._finished:
            return []
        self._finished = True
        frames: list[SSEFrame] = []
        if self._line_buffer:
            self._ensure_frame_size(len(self._line_buffer))
            frames.extend(self._process_line(bytes(self._line_buffer)))
            self._line_buffer.clear()
        if self._has_data:
            frame = self._dispatch()
            if frame is not None:
                frames.append(frame)
        return frames

    def _ensure_frame_size(self, pending_line_bytes: int) -> None:
        if self._frame_bytes + pending_line_bytes > self.max_frame_bytes:
            raise ProviderError(
                ProviderErrorKind.RESPONSE_TOO_LARGE,
                "provider stream frame exceeded the configured size limit",
            )

    def _process_line(self, raw_line: bytes) -> list[SSEFrame]:
        self._frame_bytes += len(raw_line) + 1
        if self._frame_bytes > self.max_frame_bytes:
            raise ProviderError(
                ProviderErrorKind.RESPONSE_TOO_LARGE,
                "provider stream frame exceeded the configured size limit",
            )

        if raw_line.endswith(b"\r"):
            raw_line = raw_line[:-1]
        try:
            line = raw_line.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProviderError(
                ProviderErrorKind.PROTOCOL,
                "provider stream contained invalid UTF-8",
            ) from exc

        if self._at_stream_start:
            self._at_stream_start = False
            if line.startswith("\ufeff"):
                line = line[1:]

        if not line:
            frame = self._dispatch()
            return [frame] if frame is not None else []
        if line.startswith(":"):
            return []

        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data_lines.append(value)
            self._has_data = True
        elif field == "event":
            self._event = value or None
        elif field == "id" and "\x00" not in value:
            self._last_event_id = value
        # Unknown fields and retry hints are intentionally ignored.
        return []

    def _dispatch(self) -> SSEFrame | None:
        frame = None
        if self._has_data:
            frame = SSEFrame(
                event=self._event,
                data="\n".join(self._data_lines),
                id=self._last_event_id,
            )
        self._data_lines.clear()
        self._has_data = False
        self._event = None
        self._frame_bytes = 0
        return frame
