from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from patchloop.memory import (
    MEMORY_SCHEMA_VERSION,
    CompressionOperation,
    CompressionReport,
    MemoryBundle,
    MemoryHit,
    MemoryKind,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemorySource,
    MemorySourceKind,
    MemoryStatus,
    validate_supersession_chain,
)

NOW = datetime(2026, 8, 30, tzinfo=UTC)


def source(source_id: str = "source-1") -> MemorySource:
    return MemorySource(
        id=source_id,
        task_id="task-1",
        kind=MemorySourceKind.TOOL_RESULT,
        evidence_hash="a" * 64,
        event_id="event-1",
        step_index=3,
        tool_call_id="call-1",
        path="src/service.py",
        line_start=10,
        line_end=12,
        captured_at=NOW,
    )


def record(
    record_id: str = "record-1",
    *,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    supersedes_id: str | None = None,
    superseded_by_id: str | None = None,
    created_at: datetime = NOW,
) -> MemoryRecord:
    return MemoryRecord(
        id=record_id,
        task_id="task-1",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.TASK,
        scope_id="task-1",
        content={"fact": f"contract-{record_id}"},
        retrieval_text=f"Active API contract {record_id}",
        source_ids=["source-1"],
        importance=0.9,
        confidence=0.8,
        status=status,
        supersedes_id=supersedes_id,
        superseded_by_id=superseded_by_id,
        estimated_tokens=12,
        created_at=created_at,
    )


def test_memory_contracts_round_trip_with_explicit_v1_schema() -> None:
    evidence = source()
    memory = record()
    query = MemoryQuery(
        task_id="task-1",
        text="current API contract",
        kinds=[MemoryKind.SEMANTIC],
        token_budget=100,
    )
    hit = MemoryHit(
        record=memory,
        rank=1,
        score=0.92,
        relevance_score=0.95,
        recency_score=0.8,
        source_quality_score=1.0,
        reason="matches API contract query",
        matched_terms=["api", "contract"],
        estimated_tokens=12,
    )
    bundle = MemoryBundle(query=query, hits=[hit], sources=[evidence])
    report = CompressionReport(
        id="compression-1",
        task_id="task-1",
        generation=1,
        operations=[CompressionOperation.DEDUPLICATE, CompressionOperation.SUMMARIZE],
        input_record_ids=["record-1", "record-2"],
        output_record_ids=["record-3"],
        removed_record_ids=["record-2"],
        input_tokens=100,
        output_tokens=40,
        created_at=NOW,
    )

    for model in (evidence, memory, query, hit, bundle, report):
        restored = type(model).model_validate_json(model.model_dump_json())
        assert restored == model
        assert restored.model_dump()["schema_version"] == MEMORY_SCHEMA_VERSION
    assert bundle.estimated_tokens == 12
    assert report.saved_tokens == 60
    assert report.compression_ratio == 0.4
    with pytest.raises(ValidationError, match="frozen"):
        memory.retrieval_text = "mutated"  # type: ignore[misc]


def test_unknown_versions_types_and_fields_fail_closed() -> None:
    legacy_v1 = MemoryQuery.model_validate({"task_id": "task-1", "text": "query"})
    assert legacy_v1.schema_version == MEMORY_SCHEMA_VERSION

    with pytest.raises(ValidationError, match="schema_version"):
        MemoryQuery.model_validate(
            {
                "schema_version": "2.0",
                "task_id": "task-1",
                "text": "query",
            }
        )
    payload = record().model_dump(mode="json")
    payload["kind"] = "procedural"
    with pytest.raises(ValidationError, match="kind"):
        MemoryRecord.model_validate(payload)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        MemoryQuery.model_validate({"task_id": "task-1", "text": "query", "future_field": True})


def test_long_term_memory_requires_source_but_working_memory_can_start_empty() -> None:
    payload = record().model_dump(mode="json")
    payload["source_ids"] = []
    with pytest.raises(ValidationError, match="long-term memory requires"):
        MemoryRecord.model_validate(payload)

    working = MemoryRecord(
        id="working-1",
        task_id="task-1",
        kind=MemoryKind.WORKING,
        scope_id="task-1",
        content={"active_error": "test failure"},
        retrieval_text="Current active test failure",
    )
    assert working.source_ids == []


def test_source_and_record_lifecycle_reject_invalid_states() -> None:
    with pytest.raises(ValidationError, match="required locator"):
        MemorySource(
            task_id="task-1",
            kind=MemorySourceKind.EVENT,
            evidence_hash="b" * 64,
        )
    with pytest.raises(ValidationError, match="superseded memory requires"):
        record(status=MemoryStatus.SUPERSEDED)
    with pytest.raises(ValidationError, match="only superseded memory"):
        record(superseded_by_id="record-2")
    with pytest.raises(ValidationError, match="working memory must be task-scoped"):
        MemoryRecord(
            id="working-1",
            task_id="task-1",
            kind=MemoryKind.WORKING,
            scope=MemoryScope.REPOSITORY,
            scope_id="repo-1",
            content={"goal": "fix bug"},
            retrieval_text="Active goal",
        )


def test_content_hash_detects_semantic_payload_tampering() -> None:
    original = record()
    assert len(original.content_hash) == 64
    payload = original.model_dump(mode="json")
    payload["content"] = {"fact": "silently changed"}

    with pytest.raises(ValidationError, match="content_hash does not match"):
        MemoryRecord.model_validate(payload)


def test_supersession_chain_is_bidirectional_ordered_and_acyclic() -> None:
    old = record(
        "record-old",
        status=MemoryStatus.SUPERSEDED,
        superseded_by_id="record-new",
    )
    new = record(
        "record-new",
        supersedes_id="record-old",
        created_at=NOW + timedelta(seconds=1),
    )
    validate_supersession_chain([old, new])

    broken_payload = old.model_dump(mode="json")
    broken_payload["superseded_by_id"] = "record-missing"
    broken = MemoryRecord.model_validate(broken_payload)
    with pytest.raises(ValueError, match="not bidirectional"):
        validate_supersession_chain([broken, new])

    cycle = [
        record(
            "record-a",
            status=MemoryStatus.SUPERSEDED,
            supersedes_id="record-c",
            superseded_by_id="record-b",
        ),
        record(
            "record-b",
            status=MemoryStatus.SUPERSEDED,
            supersedes_id="record-a",
            superseded_by_id="record-c",
        ),
        record(
            "record-c",
            status=MemoryStatus.SUPERSEDED,
            supersedes_id="record-b",
            superseded_by_id="record-a",
        ),
    ]
    with pytest.raises(ValueError, match="cycle"):
        validate_supersession_chain(cycle)


def test_bundle_enforces_query_provenance_rank_and_budget() -> None:
    query = MemoryQuery(
        task_id="task-1",
        text="contract",
        kinds=[MemoryKind.SEMANTIC],
        min_confidence=0.7,
        token_budget=20,
    )
    hit = MemoryHit(
        record=record(),
        rank=1,
        score=1.0,
        relevance_score=1.0,
        recency_score=1.0,
        source_quality_score=1.0,
        reason="exact match",
        estimated_tokens=12,
    )
    MemoryBundle(query=query, hits=[hit], sources=[source()])

    with pytest.raises(ValidationError, match="missing sources"):
        MemoryBundle(query=query, hits=[hit])
    oversized = hit.model_copy(update={"estimated_tokens": 21})
    with pytest.raises(ValidationError, match="token budget"):
        MemoryBundle(query=query, hits=[oversized], sources=[source()])

    scoped_query = query.model_copy(update={"scope": MemoryScope.REPOSITORY, "scope_id": "repo-1"})
    with pytest.raises(ValidationError, match="outside query scope"):
        MemoryBundle(query=scoped_query, hits=[hit], sources=[source()])


def test_compression_report_protects_inputs_and_never_expands() -> None:
    with pytest.raises(ValidationError, match="cannot be removed"):
        CompressionReport(
            task_id="task-1",
            generation=1,
            operations=[CompressionOperation.PRUNE],
            input_record_ids=["record-1"],
            output_record_ids=["record-2"],
            protected_record_ids=["record-1"],
            removed_record_ids=["record-1"],
            input_tokens=10,
            output_tokens=5,
        )
    with pytest.raises(ValidationError, match="cannot exceed"):
        CompressionReport(
            task_id="task-1",
            generation=1,
            operations=[CompressionOperation.SUMMARIZE],
            input_record_ids=["record-1"],
            output_record_ids=["record-2"],
            input_tokens=10,
            output_tokens=11,
        )
