from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src" / "patchloop"


def _absolute_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module)
    return imports


def _files_under(relative_directory: str) -> list[Path]:
    return sorted((SOURCE_ROOT / relative_directory).rglob("*.py"))


def test_memory_and_providers_do_not_depend_on_prompt_cache() -> None:
    for relative_directory in ("memory", "providers"):
        imported = {
            name for path in _files_under(relative_directory) for name in _absolute_imports(path)
        }
        assert not any(
            name == "patchloop.prompt_cache" or name.startswith("patchloop.prompt_cache.")
            for name in imported
        )


def test_prompt_cache_does_not_depend_on_runtime_persistence_or_observability() -> None:
    imported = {name for path in _files_under("prompt_cache") for name in _absolute_imports(path)}
    forbidden = ("patchloop.runtime", "patchloop.persistence", "patchloop.observability")
    assert not any(
        name == target or name.startswith(target + ".") for name in imported for target in forbidden
    )
