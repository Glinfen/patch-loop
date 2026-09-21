"""Persistent, policy-controlled Git workspaces."""

from typing import TYPE_CHECKING, Any

from patchloop.workspace.git import GitAdapter, GitError, GitParseError, GitTimeout
from patchloop.workspace.models import WorkspaceHandle, WorkspaceMode, WorkspaceStatus
from patchloop.workspace.store import WorkspaceStore

if TYPE_CHECKING:
    from patchloop.workspace.service import WorkspaceService


def __getattr__(name: str) -> Any:
    if name == "WorkspaceService":
        from patchloop.workspace.service import WorkspaceService

        return WorkspaceService
    raise AttributeError(name)


__all__ = [
    "GitAdapter",
    "GitError",
    "GitParseError",
    "GitTimeout",
    "WorkspaceHandle",
    "WorkspaceMode",
    "WorkspaceService",
    "WorkspaceStatus",
    "WorkspaceStore",
]
