"""Explainable repository intelligence tool."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from patchloop.intelligence import RepositoryIndexer, RepositorySearch, RepositorySnapshot
from patchloop.tools.base import Tool, ToolContext, ToolInputModel


class SearchCodeInput(ToolInputModel):
    query: str = Field(min_length=1)
    max_results: int = Field(default=10, ge=1, le=50)


class SearchCodeTool(Tool):
    name = "search_code"
    description = (
        "Search Python code using AST symbols, references, lightweight semantics, and "
        "source/test relationships. Returns scored evidence with ranking reasons."
    )
    input_model = SearchCodeInput

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        request = SearchCodeInput.model_validate(arguments)
        indexer = RepositoryIndexer(context.repository)
        index_path = context.repository / ".patchloop" / "repository-index.json"
        snapshot: RepositorySnapshot | None = None
        if index_path.is_file():
            try:
                loaded = RepositorySnapshot.load(index_path)
                if indexer.is_current(loaded):
                    snapshot = loaded
            except (OSError, ValueError):
                pass
        if snapshot is None:
            snapshot = indexer.build()
        search = RepositorySearch(context.repository, snapshot)
        hits = search.search(
            request.query,
            limit=request.max_results,
            recent_paths=context.recent_paths,
        )
        for hit in reversed(hits):
            context.remember_access(context.resolve_path(hit.path))
        return json.dumps(
            [hit.model_dump(mode="json") for hit in hits],
            ensure_ascii=False,
            indent=2,
        )
