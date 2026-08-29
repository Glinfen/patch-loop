"""Repository-scoped atomic write tools."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from patchloop.tools.base import PermissionLevel, Tool, ToolContext, ToolInputModel


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.patchloop-{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class CreateFileInput(ToolInputModel):
    path: str = Field(min_length=1)
    content: str


class CreateFileTool(Tool):
    name = "create_file"
    description = "Create a new UTF-8 file without overwriting an existing file."
    input_model = CreateFileInput
    permission = PermissionLevel.WRITE

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        request = CreateFileInput.model_validate(arguments)
        path = context.resolve_path(request.path, must_exist=False)
        if path.exists():
            raise ValueError(f"file already exists: {request.path}")
        if not path.parent.is_dir():
            raise ValueError(f"parent directory does not exist: {request.path}")
        context.changes.capture(path)
        _atomic_write(path, request.content)
        return f"created {request.path} ({len(request.content)} characters)"


class ReplaceTextInput(ToolInputModel):
    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1)
    new_text: str
    expected_occurrences: int = Field(default=1, ge=1, le=1_000)


class ReplaceTextTool(Tool):
    name = "replace_text"
    description = "Atomically replace an exact text fragment with an occurrence-count guard."
    input_model = ReplaceTextInput
    permission = PermissionLevel.WRITE

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        request = ReplaceTextInput.model_validate(arguments)
        path = context.resolve_path(request.path)
        if not path.is_file():
            raise ValueError(f"not a file: {request.path}")
        content = path.read_text(encoding="utf-8")
        occurrences = content.count(request.old_text)
        if occurrences != request.expected_occurrences:
            raise ValueError(
                f"expected {request.expected_occurrences} occurrences, found {occurrences}"
            )
        updated = content.replace(
            request.old_text,
            request.new_text,
            request.expected_occurrences,
        )
        context.changes.capture(path)
        _atomic_write(path, updated)
        return f"updated {request.path} ({occurrences} replacement(s))"


class TextEditInput(ToolInputModel):
    old_text: str = Field(min_length=1)
    new_text: str
    expected_occurrences: int = Field(default=1, ge=1, le=1_000)


class ApplyPatchInput(ToolInputModel):
    path: str = Field(min_length=1)
    edits: list[TextEditInput] = Field(min_length=1, max_length=100)


class ApplyPatchTool(Tool):
    name = "apply_patch"
    description = (
        "Atomically apply multiple exact text edits to one file; if any occurrence guard "
        "fails, no edit is written."
    )
    input_model = ApplyPatchInput
    permission = PermissionLevel.WRITE

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        request = ApplyPatchInput.model_validate(arguments)
        path = context.resolve_path(request.path)
        if not path.is_file():
            raise ValueError(f"not a file: {request.path}")
        updated = path.read_text(encoding="utf-8")
        replacement_count = 0
        for index, edit in enumerate(request.edits):
            occurrences = updated.count(edit.old_text)
            if occurrences != edit.expected_occurrences:
                raise ValueError(
                    f"edit {index}: expected {edit.expected_occurrences} occurrences, "
                    f"found {occurrences}"
                )
            updated = updated.replace(
                edit.old_text,
                edit.new_text,
                edit.expected_occurrences,
            )
            replacement_count += occurrences
        context.changes.capture(path)
        _atomic_write(path, updated)
        return (
            f"patched {request.path} ({len(request.edits)} edit(s), "
            f"{replacement_count} replacement(s))"
        )


class GetDiffInput(ToolInputModel):
    pass


class GetDiffTool(Tool):
    name = "get_diff"
    description = "Return a unified diff for files changed through this tool context."
    input_model = GetDiffInput

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        GetDiffInput.model_validate(arguments)
        return context.changes.diff() or "No changes."
