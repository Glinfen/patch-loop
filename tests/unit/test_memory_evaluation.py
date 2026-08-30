from pathlib import Path

import pytest

from patchloop.evaluation import (
    MemoryBenchmarkMode,
    MemoryBenchmarkRunner,
    MemoryBenchmarkVariant,
    MemoryFactDefinition,
    MemoryFactKind,
    MemoryTaskDefinition,
    MemoryTaskManifest,
    load_memory_manifest,
)
from patchloop.providers import FakeProvider, ModelResponse, ModelUsage


def memory_task(*, fact_step: int = 1) -> MemoryTaskDefinition:
    return MemoryTaskDefinition(
        id="early-fact",
        goal="At the probe, report the exact value for fact id required-value.",
        history_steps=20,
        context_budget_tokens=2_000,
        max_tool_output_chars=300,
        recent_steps=2,
        noise_chars=180,
        facts=[
            MemoryFactDefinition(
                id="required-value",
                value="PRESERVE_THIS_EARLY_FACT__T01",
                kind=MemoryFactKind.CONSTRAINT,
                introduced_at_step=fact_step,
                required_at_steps=[20],
                recall_queries=["What is the required value?"],
            )
        ],
    )


def manifest(task: MemoryTaskDefinition) -> MemoryTaskManifest:
    return MemoryTaskManifest(
        suite_id="memory-test-suite",
        revision="test-v1",
        tasks=[task],
    )


def test_task_memory_retains_early_fact_that_recent_only_loses() -> None:
    task_manifest = manifest(memory_task())
    runner = MemoryBenchmarkRunner(repeats=2)

    recent = runner.run(
        task_manifest,
        variant=MemoryBenchmarkVariant.RECENT_ONLY,
        mode=MemoryBenchmarkMode.DETERMINISTIC,
    )
    task_memory = runner.run(
        task_manifest,
        variant=MemoryBenchmarkVariant.TASK_MEMORY_V1,
        mode=MemoryBenchmarkMode.DETERMINISTIC,
    )

    assert recent.critical_fact_recall == 0.0
    assert task_memory.critical_fact_recall == 1.0
    assert task_memory.success_rate == 1.0
    assert task_memory.context_overflows == 0
    assert task_memory.stable_outcomes_across_repeats
    assert task_memory.success_rate_by_repeat == [1.0, 1.0]


def test_stale_fact_metric_detects_invalidated_value_in_context() -> None:
    task = MemoryTaskDefinition(
        id="stale-fact",
        goal="Report current-value and ignore old-value.",
        history_steps=20,
        context_budget_tokens=2_000,
        max_tool_output_chars=300,
        recent_steps=4,
        noise_chars=100,
        facts=[
            MemoryFactDefinition(
                id="old-value",
                value="STALE_VALUE_SHOULD_NOT_SURVIVE__OLD",
                introduced_at_step=18,
                valid_until_step=20,
                recall_queries=["Which value is obsolete?"],
                superseded_by="current-value",
            ),
            MemoryFactDefinition(
                id="current-value",
                value="CURRENT_VALUE_MUST_SURVIVE__NEW",
                introduced_at_step=20,
                required_at_steps=[20],
                recall_queries=["Which value is current?"],
            ),
        ],
    )

    report = MemoryBenchmarkRunner().run(
        manifest(task),
        variant=MemoryBenchmarkVariant.RECENT_ONLY,
        mode=MemoryBenchmarkMode.DETERMINISTIC,
    )

    assert report.critical_fact_recall == 1.0
    assert report.stale_fact_rate == 1.0
    assert report.success_rate == 0.0
    assert report.results[0].stale_fact_ids == ["old-value"]


def test_model_probe_records_provider_usage_and_exact_recall() -> None:
    task = memory_task(fact_step=20)
    provider = FakeProvider(
        [
            ModelResponse(
                content="PRESERVE_THIS_EARLY_FACT__T01",
                usage=ModelUsage(input_tokens=500, output_tokens=20, cost_usd=0.01),
            )
        ]
    )

    report = MemoryBenchmarkRunner(lambda: provider).run(
        manifest(task),
        variant=MemoryBenchmarkVariant.RECENT_ONLY,
        mode=MemoryBenchmarkMode.MODEL,
    )

    assert report.provider == "fake"
    assert report.success_rate == 1.0
    assert report.provider_input_tokens == 500
    assert report.provider_output_tokens == 20
    assert report.total_cost_usd == 0.01
    assert report.min_success_rate == 1.0
    assert report.max_success_rate == 1.0
    assert provider.requests
    assert [message.role for message in provider.requests[0][0]] == ["system", "user"]
    assert "MEMORY_BENCHMARK_TRANSCRIPT_V1" in provider.requests[0][0][1].content


def test_manifest_rejects_invalid_lifecycle_and_reserved_variant() -> None:
    with pytest.raises(ValueError, match="after it becomes invalid"):
        MemoryFactDefinition(
            id="invalid",
            value="INVALID_LIFECYCLE_VALUE",
            introduced_at_step=2,
            valid_until_step=5,
            required_at_steps=[5],
            recall_queries=["What is invalid?"],
        )

    with pytest.raises(ValueError, match="reserved for LCM-08"):
        MemoryBenchmarkRunner().run(
            manifest(memory_task()),
            variant=MemoryBenchmarkVariant.HIERARCHICAL_MEMORY,
            mode=MemoryBenchmarkMode.DETERMINISTIC,
        )


def test_published_manifest_covers_all_required_history_lengths() -> None:
    root = Path(__file__).parents[2]
    published = load_memory_manifest(root / "benchmarks" / "memory_tasks.json")

    assert len(published.tasks) == 8
    assert {task.history_steps for task in published.tasks} == {20, 50, 80, 120}
    assert all(task.facts for task in published.tasks)
    assert all(
        fact.required_at_steps or fact.valid_until_step is not None
        for task in published.tasks
        for fact in task.facts
    )
