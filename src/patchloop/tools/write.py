"""Repository-scoped atomic write tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import BaseModel, Field

from patchloop.tools.base import PermissionLevel, Tool, ToolContext, ToolInputModel

_UNSET_FILE_CONTENT = object()


def supports_file_mutation_preview(tool_name: str) -> bool:
    """Return whether a WRITE tool has a deterministic repository-file preview."""

    return tool_name in {
        CreateFileTool.name,
        WriteFileTool.name,
        ReplaceTextTool.name,
        ApplyPatchTool.name,
    }


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.patchloop-{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class FileMutationPreview:
    path: Path
    relative_path: str
    original_content: str | None
    target_content: str


def preview_file_mutation(
    tool_name: str,
    arguments: BaseModel | dict[str, object],
    context: ToolContext,
    *,
    current_content: str | object | None = _UNSET_FILE_CONTENT,
) -> FileMutationPreview:
    """Validate a file mutation and calculate its result without writing it."""

    if tool_name == CreateFileTool.name:
        create_request = CreateFileInput.model_validate(arguments)
        path = context.resolve_path(create_request.path, must_exist=False)
        exists = (
            path.exists() if current_content is _UNSET_FILE_CONTENT else current_content is not None
        )
        if exists:
            raise ValueError(f"file already exists: {create_request.path}")
        if not path.parent.is_dir():
            raise ValueError(f"parent directory does not exist: {create_request.path}")
        original = None
        target = create_request.content
    elif tool_name == WriteFileTool.name:
        write_request = WriteFileInput.model_validate(arguments)
        path = context.resolve_path(
            write_request.path, must_exist=current_content is _UNSET_FILE_CONTENT
        )
        if current_content is _UNSET_FILE_CONTENT:
            if not path.is_file():
                raise ValueError(f"not a file: {write_request.path}")
            original = path.read_text(encoding="utf-8")
        elif current_content is None:
            raise ValueError(f"not a file: {write_request.path}")
        else:
            original = cast(str, current_content)
        target = write_request.content
    elif tool_name == ReplaceTextTool.name:
        replace_request = ReplaceTextInput.model_validate(arguments)
        path = context.resolve_path(
            replace_request.path, must_exist=current_content is _UNSET_FILE_CONTENT
        )
        if current_content is _UNSET_FILE_CONTENT:
            if not path.is_file():
                raise ValueError(f"not a file: {replace_request.path}")
            original = path.read_text(encoding="utf-8")
        elif current_content is None:
            raise ValueError(f"not a file: {replace_request.path}")
        else:
            original = cast(str, current_content)
        occurrences = original.count(replace_request.old_text)
        if occurrences != replace_request.expected_occurrences:
            raise ValueError(
                f"expected {replace_request.expected_occurrences} occurrences, found {occurrences}"
            )
        target = original.replace(
            replace_request.old_text,
            replace_request.new_text,
            replace_request.expected_occurrences,
        )
    elif tool_name == ApplyPatchTool.name:
        patch_request = ApplyPatchInput.model_validate(arguments)
        path = context.resolve_path(
            patch_request.path, must_exist=current_content is _UNSET_FILE_CONTENT
        )
        if current_content is _UNSET_FILE_CONTENT:
            if not path.is_file():
                raise ValueError(f"not a file: {patch_request.path}")
            original = path.read_text(encoding="utf-8")
        elif current_content is None:
            raise ValueError(f"not a file: {patch_request.path}")
        else:
            original = cast(str, current_content)
        target = original
        for index, edit in enumerate(patch_request.edits):
            occurrences = target.count(edit.old_text)
            if occurrences != edit.expected_occurrences:
                raise ValueError(
                    f"edit {index}: expected {edit.expected_occurrences} occurrences, "
                    f"found {occurrences}"
                )
            target = target.replace(edit.old_text, edit.new_text, edit.expected_occurrences)
    else:
        raise ValueError(f"not a file mutation tool: {tool_name}")
    return FileMutationPreview(
        path=path,
        relative_path=path.relative_to(context.repository).as_posix(),
        original_content=original,
        target_content=target,
    )


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
        preview = preview_file_mutation(self.name, request, context)
        context.changes.capture_original(preview.path, preview.original_content)
        _atomic_write(preview.path, preview.target_content)
        return f"created {request.path} ({len(request.content)} characters)"


class WriteFileInput(ToolInputModel):
    path: str = Field(min_length=1)
    content: str


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Atomically replace an existing UTF-8 file with complete content. Use this when "
        "an exact edit cannot match a redacted observation; it never creates new files."
    )
    input_model = WriteFileInput
    permission = PermissionLevel.WRITE

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        request = WriteFileInput.model_validate(arguments)
        preview = preview_file_mutation(self.name, request, context)
        context.changes.capture_original(preview.path, preview.original_content)
        _atomic_write(preview.path, preview.target_content)
        return f"wrote {request.path} ({len(request.content)} characters)"


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
        preview = preview_file_mutation(self.name, request, context)
        if preview.original_content is None:
            raise ValueError(f"file does not exist: {request.path}")
        occurrences = preview.original_content.count(request.old_text)
        context.changes.capture_original(preview.path, preview.original_content)
        _atomic_write(preview.path, preview.target_content)
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
        preview = preview_file_mutation(self.name, request, context)
        replacement_count = sum(edit.expected_occurrences for edit in request.edits)
        context.changes.capture_original(preview.path, preview.original_content)
        _atomic_write(preview.path, preview.target_content)
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
