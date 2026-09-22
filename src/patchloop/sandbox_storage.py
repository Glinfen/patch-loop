"""Fail-closed validation for capacity-bounded Docker workspaces."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceCapacityError(RuntimeError):
    """The repository is not a verified, dedicated capacity-bounded filesystem."""


class WorkspaceCapacityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_root: str
    mount_id: int = Field(gt=0)
    device: str = Field(pattern=r"^\d+:\d+$")
    fstype: str
    total_bytes: int = Field(gt=0)
    total_inodes: int = Field(gt=0)
    limit_bytes: int = Field(gt=0)
    inode_limit: int = Field(gt=0)
    root_device: int = Field(ge=0)
    root_inode: int = Field(gt=0)
    verified_at: datetime


class _MountInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mount_id: int
    device: str
    root: str
    mount_point: str
    mount_options: frozenset[str]
    optional_fields: tuple[str, ...]
    fstype: str
    super_options: frozenset[str]


def _unescape_mountinfo(value: str) -> str:
    """Decode the octal escapes defined by proc(5), without interpreting others."""

    replacements = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and value[index + 1 : index + 4] in replacements:
            output.append(replacements[value[index + 1 : index + 4]])
            index += 4
        else:
            output.append(value[index])
            index += 1
    return "".join(output)


def _read_mountinfo(path: Path = Path("/proc/self/mountinfo")) -> list[_MountInfo]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise WorkspaceCapacityError(f"could not read {path}: {exc}") from exc
    mounts: list[_MountInfo] = []
    for line_number, line in enumerate(lines, 1):
        fields = line.split()
        try:
            separator = fields.index("-")
            if separator < 6 or len(fields) < separator + 4:
                raise ValueError("missing fields")
            mounts.append(
                _MountInfo(
                    mount_id=int(fields[0]),
                    device=fields[2],
                    root=_unescape_mountinfo(fields[3]),
                    mount_point=_unescape_mountinfo(fields[4]),
                    mount_options=frozenset(fields[5].split(",")),
                    optional_fields=tuple(fields[6:separator]),
                    fstype=fields[separator + 1],
                    super_options=frozenset(fields[separator + 3].split(",")),
                )
            )
        except (ValueError, IndexError) as exc:
            raise WorkspaceCapacityError(f"malformed mountinfo line {line_number}") from exc
    return mounts


def _same_path(candidate: str, root: Path) -> bool:
    return os.path.normcase(os.path.normpath(candidate)) == os.path.normcase(str(root))


def _is_below(candidate: str, root: Path) -> bool:
    try:
        common = os.path.commonpath([os.path.normcase(candidate), os.path.normcase(str(root))])
    except (ValueError, OSError):
        return False
    return common == os.path.normcase(str(root)) and not _same_path(candidate, root)


def _root_identity(root: Path) -> tuple[int, int]:
    stat_result = root.stat()
    return stat_result.st_dev, stat_result.st_ino


def verify_workspace_capacity(
    repository: Path,
    *,
    limit_bytes: int,
    inode_limit: int,
) -> WorkspaceCapacityEvidence:
    """Verify that *repository* is the root of a dedicated bounded ext4 mount.

    This function only validates an administrator-provisioned filesystem.  It never
    mounts, resizes, copies, or changes permissions on the repository.
    """

    if limit_bytes <= 0 or inode_limit <= 0:
        raise ValueError("workspace capacity limits must be positive")
    if os.name != "posix":
        raise WorkspaceCapacityError("workspace capacity verification requires Linux")
    try:
        canonical = repository.resolve(strict=True)
        if not canonical.is_dir():
            raise WorkspaceCapacityError("repository is not a directory")
        before_device, before_inode = _root_identity(canonical)
        mounts = _read_mountinfo()
        matching = [mount for mount in mounts if _same_path(mount.mount_point, canonical)]
        if len(matching) != 1:
            raise WorkspaceCapacityError("repository must exactly equal one filesystem mount point")
        mount = matching[0]
        if mount.fstype != "ext4":
            raise WorkspaceCapacityError(f"filesystem must be ext4, got {mount.fstype}")
        missing = sorted({"rw", "nosuid", "nodev"} - mount.mount_options)
        if missing:
            raise WorkspaceCapacityError(f"mount is missing required options: {','.join(missing)}")
        if any(field.startswith("shared:") for field in mount.optional_fields):
            raise WorkspaceCapacityError("shared-propagation mounts are not accepted")
        nested = [item.mount_point for item in mounts if _is_below(item.mount_point, canonical)]
        if nested:
            raise WorkspaceCapacityError(
                f"repository contains nested mounts: {', '.join(sorted(nested)[:3])}"
            )
        statvfs = getattr(os, "statvfs", None)
        if statvfs is None:
            raise WorkspaceCapacityError("statvfs is unavailable on this platform")
        stats = statvfs(canonical)
        total_bytes = stats.f_blocks * stats.f_frsize
        total_inodes = stats.f_files
        if total_bytes <= 0 or total_bytes > limit_bytes:
            raise WorkspaceCapacityError(
                f"filesystem capacity {total_bytes} exceeds configured limit {limit_bytes}"
            )
        if total_inodes <= 0 or total_inodes > inode_limit:
            raise WorkspaceCapacityError(
                f"filesystem inode capacity {total_inodes} exceeds configured limit {inode_limit}"
            )
        after_device, after_inode = _root_identity(canonical)
        if (before_device, before_inode) != (after_device, after_inode):
            raise WorkspaceCapacityError("repository identity changed during verification")
    except WorkspaceCapacityError:
        raise
    except OSError as exc:
        raise WorkspaceCapacityError(f"could not inspect workspace capacity: {exc}") from exc
    return WorkspaceCapacityEvidence(
        canonical_root=str(canonical),
        mount_id=mount.mount_id,
        device=mount.device,
        fstype=mount.fstype,
        total_bytes=total_bytes,
        total_inodes=total_inodes,
        limit_bytes=limit_bytes,
        inode_limit=inode_limit,
        root_device=after_device,
        root_inode=after_inode,
        verified_at=datetime.now(UTC),
    )


def same_workspace_capacity(
    before: WorkspaceCapacityEvidence, after: WorkspaceCapacityEvidence
) -> bool:
    """Return whether two validations refer to the same immutable mount identity."""

    return (
        before.canonical_root,
        before.mount_id,
        before.device,
        before.root_device,
        before.root_inode,
        before.total_bytes,
        before.total_inodes,
        before.limit_bytes,
        before.inode_limit,
    ) == (
        after.canonical_root,
        after.mount_id,
        after.device,
        after.root_device,
        after.root_inode,
        after.total_bytes,
        after.total_inodes,
        after.limit_bytes,
        after.inode_limit,
    )


__all__ = [
    "WorkspaceCapacityError",
    "WorkspaceCapacityEvidence",
    "same_workspace_capacity",
    "verify_workspace_capacity",
]
