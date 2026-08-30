import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from patchloop.domain import Task
from patchloop.memory import (
    CompressionLevel,
    CompressionOperation,
    MemoryCompressor,
    MemoryKind,
    MemoryRecord,
    MemorySource,
    MemorySourceKind,
    MemoryStatus,
)
from patchloop.persistence import SQLiteStore

NOW = datetime(2026, 8, 30, tzinfo=UTC)


def source(task_id: str, index: int) -> MemorySource:
    return MemorySource(
        id=f"source-{index}",
        task_id=task_id,
        kind=MemorySourceKind.TOOL_RESULT,
        evidence_hash=hashlib.sha256(f"evidence-{index}".encode()).hexdigest(),
        event_id=f"event-{index}",
        tool_call_id=f"call-{index}",
        step_index=index,
        path="src/service.py",
        captured_at=NOW + timedelta(seconds=index),
    )


def record(
    task_id: str,
    index: int,
    text: str,
    *,
    kind: MemoryKind = MemoryKind.EPISODIC,
    content: dict[str, object] | None = None,
    tokens: int = 120,
    status: MemoryStatus = MemoryStatus.ACTIVE,
) -> MemoryRecord:
    return MemoryRecord.model_validate(
        {
            "id": f"record-{index}",
            "task_id": task_id,
            "kind": kind,
            "scope_id": task_id,
            "content": content
            or {
                "plan_phase": "implementation",
                "paths": ["src/service.py"],
                "outcome": "unknown",
                "reference": {"tool_name": "pytest", "step_index": index},
            },
            "retrieval_text": text,
            "source_ids": [f"source-{index}"],
            "importance": 0.7,
            "confidence": 0.8,
            "status": status,
            "estimated_tokens": tokens,
            "created_at": NOW + timedelta(seconds=index),
        }
    )


def test_four_safe_classes_survive_while_redundant_history_reaches_six_to_one() -> None:
    task_id = "task-compression"
    repeated = [
        record(
            task_id,
            index,
            "pytest passed: 48 tests",
            content={
                "plan_phase": "verification",
                "paths": ["src/service.py"],
                "outcome": "verified",
                "reference": {"tool_name": "pytest", "step_index": index},
            },
        )
        for index in range(72)
    ]
    critical = [
        record(
            task_id,
            72,
            "Never modify generated files",
            kind=MemoryKind.SEMANTIC,
            content={"fact_type": "prohibition", "fact": "never modify generated files"},
        ),
        record(
            task_id,
            73,
            "Keep the public API backward compatible",
            kind=MemoryKind.SEMANTIC,
            content={"fact_type": "constraint", "fact": "public API stays compatible"},
        ),
        record(
            task_id,
            74,
            "Migration attempt failed with an integrity error",
            content={"outcome": "failed", "paths": ["src/service.py"]},
        ),
        record(
            task_id,
            75,
            "Implement the pending migration",
            content={"plan_status": "pending", "paths": ["src/service.py"]},
        ),
        record(
            task_id,
            76,
            "The final decision is to retain the compatibility shim",
            kind=MemoryKind.SEMANTIC,
            content={"final_decision": True, "fact": "retain compatibility shim"},
        ),
    ]
    records = [*repeated, *critical]
    sources = [source(task_id, index) for index in range(len(records))]

    batch = MemoryCompressor().compress(task_id, records, sources)

    assert batch.report is not None
    assert batch.compression_ratio <= 1 / 6
    assert batch.protected_survival_rate == 1.0
    assert {item.id for item in critical}.issubset(batch.protected_record_ids)
    assert {item.id for item in critical}.issubset(batch.report.output_record_ids)
    assert len(batch.compressed_record_ids) == 71
    assert all(item.status is MemoryStatus.INVALIDATED for item in batch.writes[:71])
    summary = batch.summary_records[0]
    assert set(summary.source_ids) == {f"source-{index}" for index in range(71)}
    assert set(summary.derived_from_ids) == {f"record-{index}" for index in range(71)}
    assert summary.content["lineage_record_ids"] == summary.derived_from_ids
    assert summary.content["source_steps"] == list(range(71))
    assert summary.content["untrusted"] is True
    expected = json.loads(
        (Path(__file__).parents[2] / "benchmarks" / "results" / "lcm07_compression.json").read_text(
            encoding="utf-8"
        )
    )
    assert expected["input_tokens"] == batch.report.input_tokens
    assert expected["output_tokens"] == batch.report.output_tokens
    assert expected["compression_ratio"] == batch.compression_ratio
    assert expected["protected_survival_rate"] == batch.protected_survival_rate
    assert expected["invalidated_records"] == len(batch.compressed_record_ids)


def test_record_merge_and_episode_aggregation_are_distinct_levels() -> None:
    task_id = "task-levels"
    semantic = [
        record(
            task_id,
            index,
            f"Evidence {index} says retry_limit is three",
            kind=MemoryKind.SEMANTIC,
            content={
                "slot_key": "retry_limit",
                "normalized_value": "3",
                "evidence": f"config-{index}.toml",
            },
        )
        for index in range(3)
    ]
    episodes = [
        record(
            task_id,
            index,
            f"Investigated service behavior at step {index}",
            content={
                "plan_phase": "investigation",
                "paths": ["src/service.py"],
                "outcome": "unknown",
                "reference": {"tool_name": "rg", "step_index": index},
            },
        )
        for index in range(3, 7)
    ]
    records = [*semantic, *episodes]

    batch = MemoryCompressor().compress(
        task_id,
        records,
        [source(task_id, index) for index in range(7)],
    )

    assert batch.report is not None
    assert batch.report.operations == [
        CompressionOperation.MERGE,
        CompressionOperation.SUMMARIZE,
        CompressionOperation.PRUNE,
    ]
    assert {summary.content["level"] for summary in batch.summary_records} == {
        CompressionLevel.RECORD_MERGE.value,
        CompressionLevel.EPISODE_AGGREGATE.value,
    }


def test_compression_is_deterministic_persistent_and_replay_safe(tmp_path: Path) -> None:
    task_id = "task-store"
    sources = [source(task_id, index) for index in range(8)]
    records = [record(task_id, index, "Repeated compiler output") for index in range(8)]
    compressor = MemoryCompressor()

    first = compressor.compress(task_id, records, sources)
    replay = compressor.compress(task_id, records, sources)

    assert first == replay
    runtime = SQLiteStore(tmp_path / "patchloop.db")
    runtime.save_task(Task(id=task_id, goal="Compress memory", repository=str(tmp_path)))
    runtime.memory.save_batch(sources=sources, records=records)
    persisted = compressor.compress_store(runtime.memory, task_id)
    after_replay = compressor.compress_store(runtime.memory, task_id)

    assert persisted == first
    assert after_replay.report is None
    assert len(runtime.memory.list_compactions(task_id)) == 1
    stored = runtime.memory.list_records(task_id)
    assert len(stored) == 9
    assert sum(item.status is MemoryStatus.INVALIDATED for item in stored) == 8
    assert sum(item.status is MemoryStatus.ACTIVE for item in stored) == 1


def test_generation_rollup_preserves_complete_raw_lineage(tmp_path: Path) -> None:
    task_id = "task-generations"
    compressible = [
        record(
            task_id,
            index,
            f"stable fact group {index // 8}",
            kind=MemoryKind.SEMANTIC,
            content={"fact": f"stable fact group {index // 8}"},
        )
        for index in range(48)
    ]
    protected = record(
        task_id,
        48,
        "Do not modify the generated client",
        kind=MemoryKind.SEMANTIC,
        content={"fact_type": "prohibition", "fact": "do not modify generated client"},
    )
    records = [*compressible, protected]
    sources = [source(task_id, index) for index in range(49)]
    compressor = MemoryCompressor()
    runtime = SQLiteStore(tmp_path / "patchloop.db")
    runtime.save_task(Task(id=task_id, goal="Roll up memory", repository=str(tmp_path)))
    runtime.memory.save_batch(sources=sources, records=records)

    generation_one = compressor.compress_store(runtime.memory, task_id)
    assert generation_one.report is not None
    generation_two = compressor.compress_store(runtime.memory, task_id, generation_rollup=True)
    assert generation_two.report is not None
    generation_three = compressor.compress_store(runtime.memory, task_id, generation_rollup=True)

    assert generation_one.report.generation == 1
    assert generation_two.report.generation == 2
    assert generation_three.report is not None
    assert generation_three.report.generation == 3
    assert len(generation_three.summary_records) == 1
    assert protected in generation_two.active_outputs
    assert protected in generation_three.active_outputs
    final_lineage = generation_three.summary_records[0].content["lineage_record_ids"]
    assert isinstance(final_lineage, list)
    assert {f"record-{index}" for index in range(48)}.issubset(final_lineage)
    assert [report.generation for report in runtime.memory.list_compactions(task_id)] == [1, 2, 3]
    assert len(runtime.memory.list_records(task_id)) == 58


def test_secrets_are_redacted_from_derived_summary() -> None:
    task_id = "task-redaction"
    secret = "sk-" + "X" * 24
    records = [
        record(task_id, index, f"compiler repeated credential={secret}") for index in range(2)
    ]

    batch = MemoryCompressor().compress(
        task_id,
        records,
        [source(task_id, index) for index in range(2)],
    )

    assert batch.report is not None
    summary = batch.summary_records[0]
    assert secret not in summary.retrieval_text
    assert secret not in str(summary.content)


def test_inactive_superseded_evidence_is_untouched() -> None:
    task_id = "task-superseded"
    superseded = record(
        task_id,
        0,
        "old contract",
        kind=MemoryKind.SEMANTIC,
    ).model_copy(update={"status": MemoryStatus.SUPERSEDED, "superseded_by_id": "record-1"})
    replacement = record(task_id, 1, "new contract", kind=MemoryKind.SEMANTIC)

    batch = MemoryCompressor().compress(
        task_id,
        [superseded, replacement],
        [source(task_id, 0), source(task_id, 1)],
    )

    assert batch.report is None
    assert batch.writes == ()
    assert batch.active_outputs == (replacement,)
