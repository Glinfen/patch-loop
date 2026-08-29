"""Explainable hybrid retrieval over a repository snapshot."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from tokenize import open as open_python
from typing import ClassVar, Protocol

from patchloop.intelligence.models import (
    CodeSymbol,
    IndexedFile,
    RepositorySnapshot,
    SearchHit,
)

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+")
CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
TEST_INTENT = {"test", "regression", "verify", "specification", "coverage"}
IMPLEMENTATION_INTENT = {"implementation", "source", "production", "logic", "code"}
CHINESE_TERMS = {
    "代码": " code ",
    "定位": " search ",
    "查找": " search ",
    "除法": " divide ",
    "实现": " implementation ",
    "测试": " test ",
    "回归": " regression ",
    "错误": " error ",
    "故障": " error ",
    "持久化": " persist ",
    "存储": " store ",
    "检查点": " checkpoint ",
    "恢复": " recovery ",
    "任务": " task ",
    "函数": " function ",
    "方法": " method ",
    "引用": " reference ",
}


def tokenize(text: str) -> list[str]:
    for chinese, replacement in CHINESE_TERMS.items():
        text = text.replace(chinese, replacement)
    expanded = CAMEL_BOUNDARY.sub(" ", text.replace("_", " "))
    return [token.casefold() for token in TOKEN_PATTERN.findall(expanded)]


class SemanticEncoder(Protocol):
    @property
    def name(self) -> str: ...

    def encode(self, text: str) -> Mapping[str, float]: ...


class SparseSemanticEncoder:
    """Dependency-free semantic fallback with replaceable encoder protocol."""

    _groups: ClassVar[dict[str, set[str]]] = {
        "calculate": {"arithmetic", "calculate", "calculation", "calculator", "math"},
        "divide": {"divide", "division", "fractional", "quotient"},
        "error": {"bug", "error", "exception", "failure", "fault"},
        "function": {"callable", "function", "method", "routine"},
        "persist": {"database", "persist", "persistence", "sqlite", "storage", "store"},
        "recover": {"checkpoint", "recover", "recovery", "resume", "restore"},
        "search": {"find", "locate", "query", "retrieval", "retrieve", "search"},
        "task": {"job", "task", "work"},
        "test": TEST_INTENT | {"assert", "pytest", "testing"},
    }
    _aliases: ClassVar[dict[str, str]] = {
        alias: canonical for canonical, group in _groups.items() for alias in group
    }

    @property
    def name(self) -> str:
        return "sparse-semantic-v1"

    def encode(self, text: str) -> Mapping[str, float]:
        normalized = [self._normalize(token) for token in tokenize(text)]
        counts = Counter(normalized)
        norm = math.sqrt(sum(value * value for value in counts.values())) or 1.0
        return {token: value / norm for token, value in counts.items()}

    def _normalize(self, token: str) -> str:
        alias = self._aliases.get(token)
        if alias is not None:
            return alias
        for suffix in ("ization", "ation", "ments", "ment", "ing", "ies", "ed", "s"):
            if token.endswith(suffix) and len(token) > len(suffix) + 3:
                return token[: -len(suffix)]
        return token


def cosine(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(token, 0.0) for token, value in left.items())


class RepositorySearch:
    def __init__(
        self,
        repository: Path,
        snapshot: RepositorySnapshot,
        semantic_encoder: SemanticEncoder | None = None,
    ) -> None:
        self.repository = repository.resolve(strict=True)
        if Path(snapshot.repository) != self.repository:
            raise ValueError("repository snapshot belongs to a different repository")
        self.snapshot = snapshot
        self.semantic_encoder = semantic_encoder or SparseSemanticEncoder()
        self._files = {item.path: item for item in snapshot.files}
        self._symbols = {symbol.id: symbol for item in snapshot.files for symbol in item.symbols}
        self._source_cache: dict[str, list[str]] = {}
        self._test_to_sources: dict[str, set[str]] = {}
        self._source_to_tests: dict[str, set[str]] = {}
        for mapping in snapshot.test_mappings:
            self._test_to_sources.setdefault(mapping.test_path, set()).add(mapping.source_path)
            self._source_to_tests.setdefault(mapping.source_path, set()).add(mapping.test_path)

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        recent_paths: Sequence[str] = (),
        mode: str = "hybrid",
    ) -> list[SearchHit]:
        if not query.strip():
            raise ValueError("query must not be empty")
        if limit < 1:
            raise ValueError("limit must be positive")
        if mode not in {"text", "hybrid"}:
            raise ValueError(f"unsupported search mode: {mode}")
        query_tokens = set(tokenize(query))
        query_vector = self.semantic_encoder.encode(query)
        query_seed_ids = self._query_seed_ids(query_vector)
        recent = {path: 1.0 / (position + 1) for position, path in enumerate(recent_paths)}
        candidates: list[SearchHit] = []
        for item in self.snapshot.files:
            for symbol in item.symbols:
                hit = self._score(
                    query,
                    query_tokens,
                    query_vector,
                    query_seed_ids,
                    item,
                    symbol,
                    recent.get(item.path, 0.0),
                    mode,
                )
                if hit.score > 0:
                    candidates.append(hit)
        candidates.sort(key=lambda hit: (-hit.score, hit.path, hit.start_line))
        unique_paths: list[SearchHit] = []
        seen: set[str] = set()
        for hit in candidates:
            if hit.path in seen:
                continue
            seen.add(hit.path)
            unique_paths.append(hit)
            if len(unique_paths) >= limit:
                break
        return unique_paths

    def _score(
        self,
        query: str,
        query_tokens: set[str],
        query_vector: Mapping[str, float],
        query_seed_ids: set[str],
        item: IndexedFile,
        symbol: CodeSymbol,
        recent_access: float,
        mode: str,
    ) -> SearchHit:
        snippet = self._snippet(symbol)
        searchable_text = self._symbol_text(symbol)
        raw_document = " ".join(
            [
                item.path,
                item.module,
                symbol.name,
                symbol.qualified_name,
                symbol.docstring,
                searchable_text,
            ]
        )
        document_tokens = set(tokenize(raw_document))
        keyword = len(query_tokens & document_tokens) / max(1, len(query_tokens))
        symbol_tokens = set(tokenize(f"{symbol.name} {symbol.qualified_name}"))
        symbol_match = len(query_tokens & symbol_tokens) / max(1, len(query_tokens))
        path_match = len(query_tokens & set(tokenize(item.path))) / max(1, len(query_tokens))
        semantic = cosine(query_vector, self.semantic_encoder.encode(raw_document))
        symbol_distance = self._symbol_proximity(symbol, query_seed_ids)
        test_relation = self._test_relation(query_tokens, item.path, symbol)
        file_type = 1.0 if item.path.endswith(".py") else 0.0
        features = {
            "keyword": keyword,
            "symbol": symbol_match,
            "semantic": semantic,
            "path": path_match,
            "symbol_distance": symbol_distance,
            "file_type": file_type,
            "recent_access": recent_access,
            "test_relation": test_relation,
        }
        if mode == "text":
            score = keyword
            reasons = [f"keyword overlap {keyword:.2f}"] if keyword else []
            source = "text_baseline"
        else:
            score = (
                0.28 * keyword
                + 0.22 * symbol_match
                + 0.25 * semantic
                + 0.07 * path_match
                + 0.07 * symbol_distance
                + 0.03 * file_type
                + 0.02 * recent_access
                + 0.06 * test_relation
            )
            reasons = self._reasons(features)
            source = f"ast+text+{self.semantic_encoder.name}"
        return SearchHit(
            path=item.path,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            symbol=symbol.qualified_name,
            symbol_kind=symbol.kind,
            snippet=snippet,
            score=round(score, 6),
            features={name: round(value, 6) for name, value in features.items()},
            reasons=reasons,
            source=source,
        )

    def _query_seed_ids(self, query_vector: Mapping[str, float]) -> set[str]:
        seeds: set[str] = set()
        for symbol in self._symbols.values():
            if cosine(query_vector, self.semantic_encoder.encode(symbol.name)) > 0:
                seeds.add(symbol.id)
        return seeds

    def _symbol_proximity(self, candidate: CodeSymbol, seeds: set[str]) -> float:
        if candidate.id in seeds:
            return 1.0
        item = self._files[candidate.path]
        referenced = {
            target
            for reference in item.references
            if reference.scope == candidate.qualified_name
            for target in reference.target_symbol_ids
        }
        if referenced & seeds:
            return 0.8
        seed_paths = {self._symbols[seed].path for seed in seeds}
        if candidate.path in seed_paths:
            return 0.4
        related_paths = self._source_to_tests.get(
            candidate.path, set()
        ) | self._test_to_sources.get(candidate.path, set())
        if related_paths & seed_paths:
            return 0.6
        return 0.0

    def _test_relation(
        self,
        query_tokens: set[str],
        path: str,
        symbol: CodeSymbol,
    ) -> float:
        normalized_query = set(self.semantic_encoder.encode(" ".join(query_tokens)))
        wants_test = bool(normalized_query & {"test"})
        wants_implementation = bool(query_tokens & IMPLEMENTATION_INTENT)
        item = self._files[path]
        if wants_test and item.is_test:
            return 1.0
        if wants_implementation and not item.is_test:
            return 1.0
        if item.is_test and self._test_to_sources.get(path):
            source_names = {
                source_symbol.name
                for source_path in self._test_to_sources[path]
                for source_symbol in self._files[source_path].symbols
            }
            if symbol.name in source_names or query_tokens & set().union(
                *(set(tokenize(name)) for name in source_names)
            ):
                return 0.6
        return 0.0

    def _snippet(self, symbol: CodeSymbol) -> str:
        lines = self._source_lines(symbol.path)
        start = symbol.start_line - 1
        end = min(symbol.end_line, start + 12)
        return "\n".join(lines[start:end])[:2_000]

    def _symbol_text(self, symbol: CodeSymbol) -> str:
        lines = self._source_lines(symbol.path)
        start = symbol.start_line - 1
        return "\n".join(lines[start : symbol.end_line])[:50_000]

    def _source_lines(self, relative_path: str) -> list[str]:
        cached = self._source_cache.get(relative_path)
        if cached is not None:
            return cached
        path = (self.repository / relative_path).resolve(strict=True)
        path.relative_to(self.repository)
        with open_python(str(path)) as source:
            lines = source.read().splitlines()
        self._source_cache[relative_path] = lines
        return lines

    @staticmethod
    def _reasons(features: Mapping[str, float]) -> list[str]:
        labels = {
            "keyword": "keyword overlap",
            "symbol": "symbol-name match",
            "semantic": "semantic similarity",
            "path": "path match",
            "symbol_distance": "symbol-graph proximity",
            "file_type": "Python source preference",
            "recent_access": "recently accessed file",
            "test_relation": "source/test relationship",
        }
        weights = {
            "keyword": 0.28,
            "symbol": 0.22,
            "semantic": 0.25,
            "path": 0.07,
            "symbol_distance": 0.07,
            "file_type": 0.03,
            "recent_access": 0.02,
            "test_relation": 0.06,
        }
        ranked = sorted(
            features.items(),
            key=lambda item: (-(item[1] * weights[item[0]]), item[0]),
        )
        return [f"{labels[name]} {value:.2f}" for name, value in ranked if value > 0][:5]
