from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from patchloop.domain import Plan, PlanItem, StepStatus
from patchloop.memory import (
    CrossLayerMemoryRetriever,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemorySource,
    MemorySourceKind,
    MemoryStatus,
    RetrievalLayer,
    WorkingMemoryEvent,
    WorkingMemoryItem,
    WorkingMemoryItemKind,
    WorkingMemorySnapshot,
    evaluate_retrieval,
)

NOW = datetime(2026, 8, 30, tzinfo=UTC)


def _source(task_id: str, identifier: str, path: str | None, step: int) -> MemorySource:
    return MemorySource(
        id=f"source-{identifier}",
        task_id=task_id,
        kind=MemorySourceKind.TOOL_RESULT,
        evidence_hash=hashlib.sha256(identifier.encode()).hexdigest(),
        event_id=f"event-{identifier}",
        tool_call_id=f"call-{identifier}",
        step_index=step,
        path=path,
        captured_at=NOW + timedelta(seconds=step),
    )


def _record(
    task_id: str,
    identifier: str,
    text: str,
    source: MemorySource,
    *,
    kind: MemoryKind = MemoryKind.SEMANTIC,
    scope_id: str = "repo",
    status: MemoryStatus = MemoryStatus.ACTIVE,
    importance: float = 0.8,
) -> MemoryRecord:
    content: dict[str, object]
    if kind is MemoryKind.SEMANTIC:
        content = {
            "semantic_schema": "1.0",
            "fact_type": "code_symbol",
            "subject": text.split()[0],
            "predicate": "value",
            "value": text,
            "epistemic_status": "observed",
            "authority": 50,
            "slot_key": identifier,
        }
    else:
        content = {
            "episode_schema": "1.0",
            "paths": [source.path] if source.path else [],
            "reference": {"tool_name": "run_tests"},
        }
    return MemoryRecord(
        id=identifier,
        task_id=task_id,
        kind=kind,
        scope=MemoryScope.REPOSITORY if kind is MemoryKind.SEMANTIC else MemoryScope.TASK,
        scope_id=scope_id if kind is MemoryKind.SEMANTIC else task_id,
        content=content,
        retrieval_text=text,
        source_ids=[source.id],
        importance=importance,
        confidence=0.95,
        status=status,
        superseded_by_id="replacement" if status is MemoryStatus.SUPERSEDED else None,
        estimated_tokens=max(8, len(text) // 3),
        created_at=source.captured_at,
    )


def _working(task_id: str) -> WorkingMemorySnapshot:
    return WorkingMemorySnapshot(
        task_id=task_id,
        token_budget=800,
        items=[
            WorkingMemoryItem(
                key="goal",
                kind=WorkingMemoryItemKind.GOAL,
                text="Repair MODE parser timeout and preserve API contract",
                pinned=True,
            ),
            WorkingMemoryItem(
                key="error",
                kind=WorkingMemoryItemKind.ACTIVE_ERROR,
                text="parser timeout in config.py",
                pinned=True,
                step_index=8,
            ),
            WorkingMemoryItem(
                key="changed:config.py",
                kind=WorkingMemoryItemKind.CHANGED_FILE,
                text="config.py",
                step_index=8,
            ),
        ],
        phase_events=[
            WorkingMemoryEvent(
                call_id="tests-8",
                step_index=8,
                tool_name="run_tests",
                success=False,
                summary="parser timeout",
                changed_paths=["config.py"],
            )
        ],
    )


def test_query_combines_goal_plan_error_path_and_recent_action() -> None:
    task_id = "retrieval-query"
    working = _working(task_id)
    plan = Plan(
        items=[
            PlanItem(description="Fix parser contract", status=StepStatus.RUNNING),
            PlanItem(description="Old completed work", status=StepStatus.COMPLETED),
        ]
    )
    retriever = CrossLayerMemoryRetriever()

    context = retriever.retrieve(
        task_id=task_id,
        repository_scope_id="repo",
        goal="Repair MODE parser timeout",
        plan=plan,
        working=working,
        working_render="PATCHLOOP_WORKING_MEMORY_V1 parser timeout config.py",
        episodic_render=None,
        changed_paths=["src/parser.py"],
        records=[],
        sources=[],
        total_context_tokens=2_000,
    )

    assert context.query.active_plan == ["Fix parser contract"]
    assert context.query.current_errors == ["parser timeout in config.py"]
    assert context.query.target_paths == ["config.py", "src/parser.py"]
    assert context.query.recent_actions == ["run_tests parser timeout"]
    assert "Old completed work" not in context.query.text
    assert context.allocation.retrieval_tokens + context.allocation.recent_history_tokens == 2_000
    assert context.estimated_tokens <= context.allocation.retrieval_tokens


def test_hybrid_retrieval_meets_quality_diversity_scope_and_staleness_invariants() -> None:
    task_id = "retrieval-quality"
    sources = [
        _source(task_id, "mode", "config.py", 1),
        _source(task_id, "parser", "parser.py", 2),
        _source(task_id, "timeout", "timeout.py", 3),
        _source(task_id, "failure", "parser.py", 4),
        _source(task_id, "stale", "config.py", 5),
        _source(task_id, "other-scope", "config.py", 6),
        *[_source(task_id, f"noise-{index}", "logs/test.log", 10 + index) for index in range(5)],
    ]
    relevant = [
        _record(task_id, "mode-current", "config.py MODE new active contract", sources[0]),
        _record(task_id, "parser-contract", "parser.py parser API contract", sources[1]),
        _record(task_id, "timeout-budget", "timeout.py timeout retry budget", sources[2]),
        _record(
            task_id,
            "failure-episode",
            "parser.py run_tests timeout failed strategy",
            sources[3],
            kind=MemoryKind.EPISODIC,
            importance=1.0,
        ),
    ]
    stale = _record(
        task_id,
        "mode-stale",
        "config.py MODE old stale contract",
        sources[4],
        status=MemoryStatus.SUPERSEDED,
    )
    wrong_scope = _record(
        task_id,
        "wrong-repository",
        "config.py MODE new parser timeout contract",
        sources[5],
        scope_id="another-repository",
    )
    noise = [
        _record(
            task_id,
            f"noise-{index}",
            f"logs/test.log repetitive parser timeout test output {index}",
            sources[6 + index],
            importance=0.6,
        )
        for index in range(5)
    ]
    retriever = CrossLayerMemoryRetriever(max_results=9, max_per_diversity_key=2)

    context = retriever.retrieve(
        task_id=task_id,
        repository_scope_id="repo",
        goal="Use current MODE and parser API contract; fix timeout with retry budget",
        plan=Plan(items=[PlanItem(description="Repair parser timeout", status=StepStatus.RUNNING)]),
        working=_working(task_id),
        working_render="PATCHLOOP_WORKING_MEMORY_V1 current parser timeout and config.py",
        episodic_render="PATCHLOOP_EPISODIC_MEMORY_V1 active_failures parser timeout",
        changed_paths=["config.py", "parser.py", "timeout.py"],
        records=[*relevant, stale, wrong_scope, *noise],
        sources=sources,
        total_context_tokens=4_000,
    )

    selected_ids = [selection.record_id for selection in context.record_selections]
    assert stale.id not in selected_ids
    assert wrong_scope.id not in selected_ids
    diversity = [
        selection.diversity_key
        for selection in context.record_selections
        if selection.diversity_key == "path:logs/test.log"
    ]
    assert len(diversity) <= 2
    quality = evaluate_retrieval(
        context,
        {record.id for record in relevant},
        {stale.id},
        k=5,
    )
    assert quality.recall_at_k >= 0.90
    assert quality.precision_at_k >= 0.70
    assert quality.stale_fact_rate == 0.0
    expected = json.loads(
        (Path(__file__).parents[2] / "benchmarks" / "results" / "lcm06_retrieval.json").read_text(
            encoding="utf-8"
        )
    )
    assert expected["memory_recall_at_5"] == quality.recall_at_k
    assert expected["memory_precision_at_5"] == quality.precision_at_k
    assert expected["stale_fact_rate"] == quality.stale_fact_rate
    assert expected["retrieved_records"] == len(quality.retrieved_ids)
    assert context.used_tokens[RetrievalLayer.SEMANTIC] <= context.allocation.semantic_tokens
    assert context.used_tokens[RetrievalLayer.EPISODIC] <= context.allocation.episodic_tokens
    assert context.estimated_tokens <= context.allocation.retrieval_tokens
    assert "selection_reasons" in context.rendered
    assert "PATCHLOOP_RETRIEVED_LONG_TERM_V1" in context.rendered


def test_old_critical_fact_survives_more_than_one_hundred_newer_records() -> None:
    task_id = "retrieval-old-critical"
    critical_source = _source(task_id, "critical", "contract.py", 1)
    critical = _record(
        task_id,
        "critical-contract",
        "contract.py EARLY blue widget API contract",
        critical_source,
        importance=1.0,
    )
    noise_sources = [
        _source(task_id, f"late-{index}", f"noise/{index}.log", index + 2) for index in range(120)
    ]
    noise = [
        _record(
            task_id,
            f"late-{index}",
            f"routine unrelated observation {index}",
            source,
            importance=0.2,
        )
        for index, source in enumerate(noise_sources)
    ]

    context = CrossLayerMemoryRetriever(max_results=8).retrieve(
        task_id=task_id,
        repository_scope_id="repo",
        goal="Use the EARLY blue widget API contract",
        plan=None,
        working=None,
        working_render=None,
        episodic_render=None,
        changed_paths=["contract.py"],
        records=[critical, *noise],
        sources=[critical_source, *noise_sources],
        total_context_tokens=4_000,
    )

    assert critical.id in [selection.record_id for selection in context.record_selections]


def test_oversized_query_signals_are_bounded_before_rendering() -> None:
    context = CrossLayerMemoryRetriever().retrieve(
        task_id="bounded-query",
        repository_scope_id="repo",
        goal="goal " * 5_000,
        plan=Plan(
            items=[
                PlanItem(description="plan " * 500, status=StepStatus.PENDING) for _ in range(12)
            ]
        ),
        working=None,
        working_render=None,
        episodic_render=None,
        changed_paths=[f"very/long/path/{index}/file.py" for index in range(50)],
        records=[],
        sources=[],
        total_context_tokens=256,
    )

    assert len(context.query.goal) <= 1_200
    assert len(context.query.active_plan) == 8
    assert len(context.query.target_paths) == 20
    assert len(context.query.text) <= 4_000
    assert context.estimated_tokens <= context.allocation.retrieval_tokens
