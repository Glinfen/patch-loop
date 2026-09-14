from __future__ import annotations

import pytest

from patchloop.providers.contracts import ProviderError, ProviderErrorKind
from patchloop.providers.sse import SSEDecoder, SSEFrame


def test_sse_decoder_handles_split_utf8_crlf_multiline_and_heartbeat() -> None:
    decoder = SSEDecoder()
    chunks = [
        b": heartbeat\r",
        b"\nevent: delta\r\nid: 7\r\ndata: \xe4\xb8",
        b"\xad\xe6\x96\x87\xf0\x9f\x99\x82\r\ndata: second line\r\n\r\n",
        b"data: [DONE]\n\n",
    ]

    frames = [frame for chunk in chunks for frame in decoder.feed(chunk)]

    assert frames == [
        SSEFrame(event="delta", data="中文🙂\nsecond line", id="7"),
        SSEFrame(event=None, data="[DONE]", id="7"),
    ]
    assert decoder.finish() == []


def test_sse_finish_returns_residual_frame_without_claiming_protocol_completion() -> None:
    decoder = SSEDecoder()
    assert decoder.feed(b"data: final frame\n") == []

    assert decoder.finish() == [SSEFrame(event=None, data="final frame", id=None)]


def test_sse_decoder_enforces_frame_limit_and_rejects_invalid_utf8() -> None:
    decoder = SSEDecoder(max_frame_bytes=8)
    with pytest.raises(ProviderError) as oversized:
        decoder.feed(b"data: too long\n\n")
    assert oversized.value.kind is ProviderErrorKind.RESPONSE_TOO_LARGE

    invalid = SSEDecoder()
    with pytest.raises(ProviderError) as malformed:
        invalid.feed(b"data: \xff\n\n")
    assert malformed.value.kind is ProviderErrorKind.PROTOCOL


def test_sse_decoder_ignores_nul_event_ids_and_unknown_fields() -> None:
    decoder = SSEDecoder()
    frames = decoder.feed(
        b"id: prior\ndata: one\n\nid: bad\x00id\nretry: 100\nwhat: ignored\ndata: two\n\n"
    )

    assert frames == [
        SSEFrame(event=None, data="one", id="prior"),
        SSEFrame(event=None, data="two", id="prior"),
    ]
