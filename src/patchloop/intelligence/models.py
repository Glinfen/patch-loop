"""Data models for repository indexing, retrieval, and evaluation."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from patchloop.domain import utc_now


class SymbolKind(StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"


class CodeSymbol(BaseModel):
    id: str
    name: str
    qualified_name: str
    kind: SymbolKind
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    signature: str = ""
    docstring: str = ""


class CodeReference(BaseModel):
    name: str
    path: str
    line: int = Field(ge=1)
    scope: str
    target_symbol_ids: list[str] = Field(default_factory=list)


class IndexedFile(BaseModel):
    path: str
    module: str
    digest: str
    line_count: int = Field(ge=0)
    is_test: bool = False
    imports: list[str] = Field(default_factory=list)
    symbols: list[CodeSymbol] = Field(default_factory=list)
    references: list[CodeReference] = Field(default_factory=list)
    parse_error: str | None = None


class TestMapping(BaseModel):
    source_path: str
    test_path: str
    reasons: list[str] = Field(min_length=1)


class RepositorySnapshot(BaseModel):
    version: int = 1
    repository: str
    files: list[IndexedFile]
    test_mappings: list[TestMapping] = Field(default_factory=list)
    indexed_at: datetime = Field(default_factory=utc_now)

    @property
    def symbol_count(self) -> int:
        return sum(len(item.symbols) for item in self.files)

    @property
    def reference_count(self) -> int:
        return sum(len(item.references) for item in self.files)

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> RepositorySnapshot:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class SearchHit(BaseModel):
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    symbol: str
    symbol_kind: SymbolKind
    snippet: str
    score: float = Field(ge=0)
    features: dict[str, float]
    reasons: list[str]
    source: str = "repository_index"


class RetrievalTask(BaseModel):
    id: str
    repository: str
    query: str
    expected_paths: list[str] = Field(min_length=1)
    k: int = Field(default=3, ge=1, le=20)


class RetrievalTaskResult(BaseModel):
    task_id: str
    query: str
    expected_paths: list[str]
    baseline_paths: list[str]
    hybrid_paths: list[str]
    baseline_hit: bool
    hybrid_hit: bool


class RetrievalEvaluationReport(BaseModel):
    task_count: int = Field(ge=1)
    baseline_recall_at_k: float = Field(ge=0, le=1)
    hybrid_recall_at_k: float = Field(ge=0, le=1)
    improvement: float
    results: list[RetrievalTaskResult]
    generated_at: datetime = Field(default_factory=utc_now)
