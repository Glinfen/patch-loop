import json
from pathlib import Path

from patchloop.intelligence import (
    RepositoryIndexer,
    RepositorySearch,
    RepositorySnapshot,
    RetrievalEvaluationReport,
    SparseSemanticEncoder,
    SymbolKind,
    evaluate_retrieval,
    load_retrieval_tasks,
)


def make_indexed_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    (repository / "src" / "sample").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "src" / "sample" / "service.py").write_text(
        "class Calculator:\n"
        "    def divide(self, dividend: int, divisor: int) -> float:\n"
        '        """Return a quotient."""\n'
        "        return dividend / divisor\n\n"
        "async def fetch_result() -> float:\n"
        "    calculator = Calculator()\n"
        "    return calculator.divide(4, 2)\n",
        encoding="utf-8",
    )
    (repository / "tests" / "test_service.py").write_text(
        "from sample.service import Calculator\n\n"
        "def test_divide() -> None:\n"
        "    assert Calculator().divide(5, 2) == 2.5\n",
        encoding="utf-8",
    )
    (repository / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    (repository / "latin1_module.py").write_bytes(b"# -*- coding: latin-1 -*-\nLABEL = 'caf\xe9'\n")
    return repository


def test_indexer_extracts_symbols_references_errors_and_test_mappings(tmp_path: Path) -> None:
    repository = make_indexed_repository(tmp_path)

    snapshot = RepositoryIndexer(repository).build()

    service = next(item for item in snapshot.files if item.path.endswith("service.py"))
    kinds = {symbol.name: symbol.kind for symbol in service.symbols}
    assert kinds["Calculator"] is SymbolKind.CLASS
    assert kinds["divide"] is SymbolKind.METHOD
    assert kinds["fetch_result"] is SymbolKind.FUNCTION
    divide = next(symbol for symbol in service.symbols if symbol.name == "divide")
    assert divide.signature == "divide(self, dividend: int, divisor: int)"
    assert any(reference.name == "divide" for reference in service.references)
    broken = next(item for item in snapshot.files if item.path == "broken.py")
    assert broken.parse_error
    assert broken.symbols[0].kind is SymbolKind.MODULE
    assert (
        next(item for item in snapshot.files if item.path == "latin1_module.py").parse_error is None
    )
    mapping = snapshot.test_mappings[0]
    assert mapping.source_path == "src/sample/service.py"
    assert mapping.test_path == "tests/test_service.py"
    assert any("imports" in reason for reason in mapping.reasons)


def test_snapshot_round_trip_and_freshness(tmp_path: Path) -> None:
    repository = make_indexed_repository(tmp_path)
    indexer = RepositoryIndexer(repository)
    snapshot = indexer.build()
    path = snapshot.save(tmp_path / "index.json")

    loaded = RepositorySnapshot.load(path)

    assert loaded.symbol_count == snapshot.symbol_count
    assert indexer.is_current(loaded)
    (repository / "broken.py").write_text("def fixed():\n    return True\n", encoding="utf-8")
    assert not indexer.is_current(loaded)


def test_hybrid_search_ranks_implementation_and_explains_evidence(tmp_path: Path) -> None:
    repository = make_indexed_repository(tmp_path)
    search = RepositorySearch(repository, RepositoryIndexer(repository).build())

    implementation = search.search("除法实现", limit=1)
    regression = search.search("division regression test", limit=1)
    recent = search.search(
        "calculator",
        limit=2,
        recent_paths=["tests/test_service.py"],
    )

    assert implementation[0].path == "src/sample/service.py"
    assert implementation[0].start_line == 2
    assert implementation[0].source.startswith("ast+text+")
    assert "return dividend / divisor" in implementation[0].snippet
    assert any("semantic similarity" in reason for reason in implementation[0].reasons)
    assert regression[0].path == "tests/test_service.py"
    assert any(hit.features["recent_access"] > 0 for hit in recent)


def test_sparse_semantic_encoder_links_domain_synonyms() -> None:
    encoder = SparseSemanticEncoder()

    assert set(encoder.encode("quotient")) == {"divide"}
    assert set(encoder.encode("恢复检查点")) == {"recover"}


def test_retrieval_evaluation_compares_text_and_hybrid(tmp_path: Path) -> None:
    repository = make_indexed_repository(tmp_path)
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            [
                {
                    "id": "division-source",
                    "repository": "repository",
                    "query": "division implementation",
                    "expected_paths": ["src/sample/service.py"],
                    "k": 1,
                },
                {
                    "id": "division-test",
                    "repository": "repository",
                    "query": "division regression test",
                    "expected_paths": ["tests/test_service.py"],
                    "k": 1,
                },
            ]
        ),
        encoding="utf-8",
    )

    report = evaluate_retrieval(load_retrieval_tasks(tasks_path), tmp_path)

    assert repository.is_dir()
    assert report.hybrid_recall_at_k == 1.0
    assert report.hybrid_recall_at_k > report.baseline_recall_at_k


def test_committed_retrieval_report_is_reproducible() -> None:
    root = Path(__file__).parents[2]
    actual = evaluate_retrieval(
        load_retrieval_tasks(root / "benchmarks" / "retrieval_tasks.json"),
        root,
    )
    committed = RetrievalEvaluationReport.model_validate_json(
        (root / "benchmarks" / "results" / "week05_retrieval.json").read_text(encoding="utf-8")
    )

    assert actual.model_dump(exclude={"generated_at"}) == committed.model_dump(
        exclude={"generated_at"}
    )
