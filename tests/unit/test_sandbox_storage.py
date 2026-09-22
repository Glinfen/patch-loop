from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import patchloop.sandbox_storage as storage
from patchloop.sandbox_storage import WorkspaceCapacityError, verify_workspace_capacity


def _mount(
    root: Path,
    *,
    fstype: str = "ext4",
    options: str = "rw,nosuid,nodev",
    optional: tuple[str, ...] = (),
    mount_id: int = 42,
) -> storage._MountInfo:
    return storage._MountInfo(
        mount_id=mount_id,
        device="7:1",
        root="/",
        mount_point=str(root),
        mount_options=frozenset(options.split(",")),
        optional_fields=optional,
        fstype=fstype,
        super_options=frozenset({"rw"}),
    )


def _linux(monkeypatch: pytest.MonkeyPatch, root: Path, mounts: list[object]) -> None:
    monkeypatch.setattr(storage.os, "name", "posix")
    monkeypatch.setattr(storage, "_read_mountinfo", lambda: mounts)
    monkeypatch.setattr(
        storage.os,
        "statvfs",
        lambda path: SimpleNamespace(f_blocks=16_000, f_frsize=4096, f_files=4000),
        raising=False,
    )


def test_mountinfo_path_escapes_are_decoded() -> None:
    assert storage._unescape_mountinfo(r"/data/a\040b\011c\134d") == "/data/a b\tc\\d"


def test_mountinfo_reader_decodes_escaped_mountpoint(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        r"42 1 7:1 / /data/a\040b rw,nosuid,nodev - ext4 /dev/loop1 rw" + "\n",
        encoding="utf-8",
    )

    mounts = storage._read_mountinfo(mountinfo)

    assert mounts[0].mount_point == "/data/a b"


def test_verified_dedicated_ext4_mount_returns_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _linux(monkeypatch, tmp_path, [_mount(tmp_path)])

    evidence = verify_workspace_capacity(tmp_path, limit_bytes=64 * 1024 * 1024, inode_limit=4096)

    assert evidence.canonical_root == str(tmp_path.resolve())
    assert evidence.mount_id == 42
    assert evidence.total_bytes == 16_000 * 4096
    assert evidence.total_inodes == 4000


@pytest.mark.parametrize(
    ("mounts", "detail"),
    [
        ([], "exactly equal"),
        (["wrong-fstype"], "ext4"),
        (["read-only"], "required options"),
        (["shared"], "shared-propagation"),
        (["nested"], "nested mounts"),
    ],
)
def test_unqualified_mounts_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mounts: list[str],
    detail: str,
) -> None:
    values: list[storage._MountInfo]
    if mounts == ["wrong-fstype"]:
        values = [_mount(tmp_path, fstype="xfs")]
    elif mounts == ["read-only"]:
        values = [_mount(tmp_path, options="ro,nosuid,nodev")]
    elif mounts == ["shared"]:
        values = [_mount(tmp_path, optional=("shared:1",))]
    elif mounts == ["nested"]:
        values = [_mount(tmp_path), _mount(tmp_path / "child", mount_id=43)]
    else:
        values = []
    _linux(monkeypatch, tmp_path, values)

    with pytest.raises(WorkspaceCapacityError, match=detail):
        verify_workspace_capacity(tmp_path, limit_bytes=64 * 1024 * 1024, inode_limit=4096)


def test_capacity_and_inode_overflow_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _linux(monkeypatch, tmp_path, [_mount(tmp_path)])
    monkeypatch.setattr(
        storage.os,
        "statvfs",
        lambda path: SimpleNamespace(f_blocks=20_000, f_frsize=4096, f_files=5000),
        raising=False,
    )

    with pytest.raises(WorkspaceCapacityError, match="capacity"):
        verify_workspace_capacity(tmp_path, limit_bytes=64 * 1024 * 1024, inode_limit=6000)
    with pytest.raises(WorkspaceCapacityError, match="inode"):
        verify_workspace_capacity(tmp_path, limit_bytes=128 * 1024 * 1024, inode_limit=4096)


def test_root_device_or_inode_change_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _linux(monkeypatch, tmp_path, [_mount(tmp_path)])
    identities = iter([(7, 100), (7, 101)])
    monkeypatch.setattr(storage, "_root_identity", lambda root: next(identities))

    with pytest.raises(WorkspaceCapacityError, match="identity changed"):
        verify_workspace_capacity(tmp_path, limit_bytes=64 * 1024 * 1024, inode_limit=4096)
