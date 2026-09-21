"""Conservative ownership and filesystem boundary tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from patchloop.workspace.ownership import WorkspaceConflict, file_state, safe_path


@pytest.mark.parametrize(
    "selector",
    [
        "../outside",
        ".git/config",
        ".patchloop/state",
        ".env",
        ".git./config",
        "NUL.txt",
        "folder /file",
    ],
)
def test_control_plane_selectors_are_rejected(tmp_path: Path, selector: str) -> None:
    with pytest.raises((ValueError, WorkspaceConflict)):
        safe_path(tmp_path, selector)


def test_nested_repository_and_symlink_are_rejected(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".git").mkdir()
    with pytest.raises(WorkspaceConflict):
        safe_path(tmp_path, "nested/file")
    link = tmp_path / "link"
    try:
        link.symlink_to(nested, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted")
    with pytest.raises(WorkspaceConflict):
        safe_path(tmp_path, "link/file")


def test_binary_digest_and_bytes(tmp_path: Path) -> None:
    target = tmp_path / "binary"
    target.write_bytes(b"\x00\xff\r\n")
    state = file_state(target)
    assert state.kind == "binary"
    assert state.content_base64 == "AP8NCg=="
    target.write_bytes(b"\x00\xfe\r\n")
    assert file_state(target).digest != state.digest
