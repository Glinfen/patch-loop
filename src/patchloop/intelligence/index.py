"""Python AST repository index with references and source-to-test mappings."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from tokenize import open as open_python

from patchloop.intelligence.models import (
    CodeReference,
    CodeSymbol,
    IndexedFile,
    RepositorySnapshot,
    SymbolKind,
    TestMapping,
)

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


def python_files(repository: Path) -> list[Path]:
    repository = repository.resolve(strict=True)
    files: list[Path] = []
    for path in repository.rglob("*.py"):
        if any(part in IGNORED_DIRECTORIES for part in path.relative_to(repository).parts):
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(repository)
        except (OSError, ValueError):
            continue
        if resolved == path and path.is_file() and not path.is_symlink():
            files.append(path)
    return sorted(files)


def module_name(relative_path: str) -> str:
    parts = list(Path(relative_path).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts.pop(0)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def is_test_path(relative_path: str) -> bool:
    path = Path(relative_path)
    return "tests" in path.parts or path.name.startswith("test_") or path.name.endswith("_test.py")


def _module_symbol(relative_path: str, module: str, line_count: int) -> CodeSymbol:
    return CodeSymbol(
        id=f"{relative_path}:{module}:1",
        name=module.rsplit(".", 1)[-1] if module else Path(relative_path).stem,
        qualified_name=module,
        kind=SymbolKind.MODULE,
        path=relative_path,
        start_line=1,
        end_line=max(1, line_count),
    )


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str, module: str, line_count: int) -> None:
        self.relative_path = relative_path
        self.module = module
        self.symbols = [_module_symbol(relative_path, module, line_count)]
        self.references: list[CodeReference] = []
        self.imports: set[str] = set()
        self._scope: list[str] = []
        self._class_depth = 0

    @property
    def scope(self) -> str:
        return ".".join(part for part in [self.module, *self._scope] if part)

    def _definition(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualified_name = ".".join(part for part in [self.module, *self._scope, node.name] if part)
        if isinstance(node, ast.ClassDef):
            kind = SymbolKind.CLASS
            signature = node.name
        else:
            kind = SymbolKind.METHOD if self._class_depth else SymbolKind.FUNCTION
            signature = f"{node.name}({ast.unparse(node.args)})"
        self.symbols.append(
            CodeSymbol(
                id=f"{self.relative_path}:{qualified_name}:{node.lineno}",
                name=node.name,
                qualified_name=qualified_name,
                kind=kind,
                path=self.relative_path,
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                signature=signature,
                docstring=ast.get_docstring(node, clean=True) or "",
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._definition(node)
        self._scope.append(node.name)
        self._class_depth += 1
        self.generic_visit(node)
        self._class_depth -= 1
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._definition(node)
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.references.append(
                CodeReference(
                    name=node.id,
                    path=self.relative_path,
                    line=node.lineno,
                    scope=self.scope,
                )
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self.references.append(
                CodeReference(
                    name=node.attr,
                    path=self.relative_path,
                    line=node.lineno,
                    scope=self.scope,
                )
            )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.update(alias.name for alias in node.names)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.imports.add(node.module)
            self.imports.update(f"{node.module}.{alias.name}" for alias in node.names)
        self.generic_visit(node)


class RepositoryIndexer:
    def __init__(self, repository: Path) -> None:
        self.repository = repository.resolve(strict=True)
        if not self.repository.is_dir():
            raise ValueError(f"repository is not a directory: {repository}")

    def build(self) -> RepositorySnapshot:
        indexed_files = [self._index_file(path) for path in python_files(self.repository)]
        self._resolve_references(indexed_files)
        mappings = self._map_tests(indexed_files)
        return RepositorySnapshot(
            repository=str(self.repository),
            files=indexed_files,
            test_mappings=mappings,
        )

    def is_current(self, snapshot: RepositorySnapshot) -> bool:
        if Path(snapshot.repository) != self.repository:
            return False
        current = {
            path.relative_to(self.repository).as_posix(): self._digest(path)
            for path in python_files(self.repository)
        }
        indexed = {item.path: item.digest for item in snapshot.files}
        return current == indexed

    def _index_file(self, path: Path) -> IndexedFile:
        relative = path.relative_to(self.repository).as_posix()
        with open_python(str(path)) as source:
            text = source.read()
        lines = text.splitlines()
        module = module_name(relative)
        try:
            tree = ast.parse(text, filename=relative)
        except SyntaxError as exc:
            return IndexedFile(
                path=relative,
                module=module,
                digest=self._digest(path),
                line_count=len(lines),
                is_test=is_test_path(relative),
                symbols=[_module_symbol(relative, module, len(lines))],
                parse_error=f"{exc.msg} at line {exc.lineno}",
            )
        visitor = _PythonVisitor(relative, module, len(lines))
        visitor.visit(tree)
        return IndexedFile(
            path=relative,
            module=module,
            digest=self._digest(path),
            line_count=len(lines),
            is_test=is_test_path(relative),
            imports=sorted(visitor.imports),
            symbols=visitor.symbols,
            references=visitor.references,
        )

    @staticmethod
    def _resolve_references(files: list[IndexedFile]) -> None:
        by_name: dict[str, list[CodeSymbol]] = {}
        for item in files:
            for symbol in item.symbols:
                by_name.setdefault(symbol.name, []).append(symbol)
        for item in files:
            for reference in item.references:
                candidates = by_name.get(reference.name, [])
                local = [symbol for symbol in candidates if symbol.path == item.path]
                resolved = local or candidates
                reference.target_symbol_ids = [symbol.id for symbol in resolved]

    @staticmethod
    def _map_tests(files: list[IndexedFile]) -> list[TestMapping]:
        source_files = [item for item in files if not item.is_test]
        test_files = [item for item in files if item.is_test]
        mappings: list[TestMapping] = []
        for test in test_files:
            reference_names = {reference.name for reference in test.references}
            for source in source_files:
                reasons: list[str] = []
                if source.module and source.module in test.imports:
                    reasons.append(f"imports module {source.module}")
                imported_symbols = {
                    symbol.name
                    for symbol in source.symbols
                    if f"{source.module}.{symbol.name}" in test.imports
                }
                if imported_symbols:
                    reasons.append("imports symbols " + ", ".join(sorted(imported_symbols)))
                referenced = {
                    symbol.name
                    for symbol in source.symbols
                    if symbol.kind is not SymbolKind.MODULE and symbol.name in reference_names
                }
                if referenced and (source.module in test.imports or imported_symbols):
                    reasons.append("references " + ", ".join(sorted(referenced)))
                if reasons:
                    mappings.append(
                        TestMapping(
                            source_path=source.path,
                            test_path=test.path,
                            reasons=reasons,
                        )
                    )
        return mappings

    @staticmethod
    def _digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
