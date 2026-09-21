from __future__ import annotations

import subprocess
import sys
import time

import pytest

from patchloop.sandbox_output import (
    OutputCollectionInterrupted,
    OutputCollectionTimeout,
    collect_process_output,
)


def _spawn(code: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _terminate(process: subprocess.Popen[bytes], _: str) -> None:
    process.kill()
    process.wait(timeout=2)


def test_collect_process_output_is_bounded_for_large_dual_streams() -> None:
    process = _spawn(
        "import sys; "
        "sys.stdout.buffer.write(b'O' * (8 * 1024 * 1024)); sys.stdout.flush(); "
        "sys.stderr.buffer.write(b'E' * (8 * 1024 * 1024)); sys.stderr.flush()"
    )

    captured = collect_process_output(
        process,
        max_output_chars=4096,
        deadline=time.monotonic() + 10,
        interruption_probe=None,
        terminate=lambda reason: _terminate(process, reason),
    )

    assert captured.stdout_tail == "O" * 4096
    assert captured.stderr_tail == "E" * 4096
    assert captured.stdout_bytes == 8 * 1024 * 1024
    assert captured.stderr_bytes == 8 * 1024 * 1024
    assert captured.truncated is True


def test_collect_process_output_decodes_split_and_invalid_utf8() -> None:
    process = _spawn(
        "import os; "
        "os.write(1, b'prefix\\xe4\\xbd'); os.write(1, b'\\xa0\\xffsuffix'); "
        "os.write(2, b'error')"
    )

    captured = collect_process_output(
        process,
        max_output_chars=100,
        deadline=time.monotonic() + 5,
        interruption_probe=None,
        terminate=lambda reason: _terminate(process, reason),
    )

    assert captured.stdout_tail == "prefix你�suffix"
    assert captured.stderr_tail == "error"
    assert captured.stdout_bytes == 16
    assert captured.stderr_bytes == 5
    assert captured.truncated is False


@pytest.mark.parametrize("mode", ["timeout", "interrupt"])
def test_collect_process_output_stops_process_and_closes_readers(mode: str) -> None:
    process = _spawn("import sys,time; print('started', flush=True); time.sleep(30)")

    kwargs = {
        "process": process,
        "max_output_chars": 100,
        "deadline": time.monotonic() + (0.2 if mode == "timeout" else 5),
        "interruption_probe": (lambda: "cancel") if mode == "interrupt" else None,
        "terminate": lambda reason: _terminate(process, reason),
    }
    expected = OutputCollectionTimeout if mode == "timeout" else OutputCollectionInterrupted
    with pytest.raises(expected):
        collect_process_output(**kwargs)

    assert process.poll() is not None
    assert process.stdout is not None and process.stdout.closed
    assert process.stderr is not None and process.stderr.closed
