from pathlib import Path

from patchloop.evaluation import (
    MemoryBenchmarkMode,
    MemoryBenchmarkReport,
    MemoryBenchmarkRunner,
    MemoryBenchmarkVariant,
    load_memory_manifest,
)


def test_lcm00_task_memory_baseline_is_reproducible() -> None:
    root = Path(__file__).parents[2]
    manifest = load_memory_manifest(root / "benchmarks" / "memory_tasks.json")

    report = MemoryBenchmarkRunner().run(
        manifest,
        variant=MemoryBenchmarkVariant.TASK_MEMORY_V1,
        mode=MemoryBenchmarkMode.DETERMINISTIC,
    )

    assert report.total_runs == 8
    assert report.successful_runs == 7
    assert report.success_rate == 0.875
    assert report.critical_fact_recall == 1.0
    assert report.stale_fact_rate == 1.0
    assert report.repeated_failure_risk_rate == 0.0
    assert report.context_overflows == 0
    assert report.max_context_tokens_used <= 4_000
    assert report.stable_outcomes_across_repeats


def test_published_lcm00_reports_match_protocol() -> None:
    results = Path(__file__).parents[2] / "benchmarks" / "results"
    reports = {
        path.name: MemoryBenchmarkReport.model_validate_json(path.read_text(encoding="utf-8"))
        for path in (
            results / "lcm00_recent_only.json",
            results / "lcm00_task_memory_v1.json",
            results / "lcm00_recent_only_deepseek.json",
            results / "lcm00_task_memory_v1_deepseek.json",
        )
    }

    assert all(report.repeats == 3 for report in reports.values())
    assert reports["lcm00_recent_only.json"].success_rate_by_repeat == [0.0, 0.0, 0.0]
    assert reports["lcm00_task_memory_v1.json"].success_rate_by_repeat == [
        0.875,
        0.875,
        0.875,
    ]
    assert reports["lcm00_recent_only_deepseek.json"].successful_runs == 0
    model_memory = reports["lcm00_task_memory_v1_deepseek.json"]
    assert model_memory.successful_runs == 22
    assert model_memory.critical_fact_recall == 1.0
    assert model_memory.stale_fact_rate == 2 / 3
