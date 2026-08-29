"""Safe repository-scoped read-only tools."""

from __future__ import annotations

import fnmatch
from pathlib import Path

from pydantic import BaseModel, Field

from patchloop.tools.base import Tool, ToolContext, ToolInputModel

IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".patchloop",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}


def _files_under(root: Path, context: ToolContext) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or any(part in IGNORED_DIRECTORIES for part in path.parts):
            continue
        try:
            resolved = context.resolve_path(str(path.relative_to(context.repository)))
        except (OSError, ValueError):
            continue
        if resolved == path and not path.is_symlink():
            files.append(path)
    return sorted(files)


class ListFilesInput(ToolInputModel):
    path: str = "."
    pattern: str = "*"
    max_files: int = Field(default=200, ge=1, le=2_000)


class ListFilesTool(Tool):
    name = "list_files"
    description = "List files inside the repository, optionally filtered by a glob pattern."
    input_model = ListFilesInput

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        request = ListFilesInput.model_validate(arguments)
        root = context.resolve_path(request.path)
        if not root.is_dir():
            raise ValueError(f"not a directory: {request.path}")
        matches = [
            path.relative_to(context.repository).as_posix()
            for path in _files_under(root, context)
            if fnmatch.fnmatch(path.name, request.pattern)
        ]
        truncated = len(matches) > request.max_files
        matches = matches[: request.max_files]
        if truncated:
            matches.append(f"... truncated after {request.max_files} files")
        return "\n".join(matches)


class ReadFileInput(ToolInputModel):
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    max_chars: int = Field(default=20_000, ge=1, le=200_000)


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a UTF-8 text file or a selected inclusive line range."
    input_model = ReadFileInput

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        request = ReadFileInput.model_validate(arguments)
        path = context.resolve_path(request.path)
        if not path.is_file():
            raise ValueError(f"not a file: {request.path}")
        context.remember_access(path)
        if request.end_line is not None and request.end_line < request.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        lines = path.read_text(encoding="utf-8").splitlines()
        selected = lines[request.start_line - 1 : request.end_line]
        numbered = [
            f"{number}: {line}" for number, line in enumerate(selected, start=request.start_line)
        ]
        output = "\n".join(numbered)
        if len(output) > request.max_chars:
            return output[: request.max_chars] + "\n... truncated"
        return output


class SearchTextInput(ToolInputModel):
    query: str = Field(min_length=1)
    path: str = "."
    pattern: str = "*"
    case_sensitive: bool = False
    max_results: int = Field(default=50, ge=1, le=500)


class SearchTextTool(Tool):
    name = "search_text"
    description = "Search UTF-8 repository files and return file, line, and matching text."
    input_model = SearchTextInput

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        request = SearchTextInput.model_validate(arguments)
        root = context.resolve_path(request.path)
        query = request.query if request.case_sensitive else request.query.casefold()
        matches: list[str] = []
        for path in _files_under(root, context):
            if not fnmatch.fnmatch(path.name, request.pattern):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(lines, start=1):
                candidate = line if request.case_sensitive else line.casefold()
                if query in candidate:
                    relative = path.relative_to(context.repository).as_posix()
                    context.remember_access(path)
                    matches.append(f"{relative}:{line_number}:{line.strip()}")
                    if len(matches) >= request.max_results:
                        matches.append(f"... truncated after {request.max_results} results")
                        return "\n".join(matches)
        return "\n".join(matches)
