from pathlib import Path

from typer.testing import CliRunner

from patchloop.cli import app
from patchloop.evaluation import (
    ALL_CACHE_SCENARIOS,
    ALL_CACHE_VARIANTS,
    CacheBenchmarkRunner,
    RealProviderCacheCollector,
    provider_cache_capability,
    serialize_cache_controls,
)
from patchloop.evaluation.cache_simulator import DeterministicPrefixCacheSimulator
from patchloop.events import Event
from patchloop.prompt_cache import CacheLayoutReason, CacheLayoutTrace
from patchloop.providers import (
    FakeProvider,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    ToolSpec,
)

runner = CliRunner()


def test_fake_prefix_simulator_reports_exact_shared_prefix_and_repeatable_fingerprint() -> None:
    simulator = DeterministicPrefixCacheSimulator()
    tools = [ToolSpec(name="read", description="Read.", parameters={"type": "object"})]
    first = [
        ModelMessage(role="system", content="stable"),
        ModelMessage(role="user", content="one"),
    ]
    second = [*first, ModelMessage(role="assistant", content="two")]

    cold, cold_usage = simulator.observe(0, first, tools)
    warm, warm_usage = simulator.observe(1, second, tools)
    assert cold.primary_reason is CacheLayoutReason.COLD_START
    assert cold_usage.cache_hit_tokens == 0
    assert warm.longest_common_prefix_bytes > 0
    assert warm_usage.cache_hit_tokens == warm.longest_common_prefix_tokens
    assert warm_usage.cache_hit_tokens + warm_usage.cache_miss_tokens == warm_usage.input_tokens

    simulator.reset()
    repeated, _ = simulator.observe(0, first, tools)
    assert repeated.request_fingerprint == cold.request_fingerprint


def test_fake_provider_can_attach_deterministic_cache_usage() -> None:
    tools = [ToolSpec(name="read", description="Read.", parameters={"type": "object"})]
    messages = [ModelMessage(role="system", content="stable")]
    provider = FakeProvider(
        [ModelResponse(content="ok")],
        cache_simulator=DeterministicPrefixCacheSimulator(),
    )

    response = provider.complete(messages, tools)

    assert response.usage.input_tokens > 0
    assert response.usage.cache_hit_tokens == 0
    assert response.usage.cache_miss_tokens == response.usage.input_tokens


def test_cache_benchmark_has_fixed_matrix_and_stable_result_fingerprint() -> None:
    first = CacheBenchmarkRunner(repeats=3).run()
    second = CacheBenchmarkRunner(repeats=3).run()

    assert first.variants == ALL_CACHE_VARIANTS
    assert first.scenarios == ALL_CACHE_SCENARIOS
    assert len(first.runs) == len(ALL_CACHE_VARIANTS) * 3
    assert first.deterministic_fingerprint == second.deterministic_fingerprint
    assert all(len(run.steps) == len(ALL_CACHE_SCENARIOS) for run in first.runs)
    assert all(summary.run_count == 3 for summary in first.summaries)
    assert "PCO-06 cache matrix" in first.human_summary()


def test_provider_collector_preserves_reported_fields_without_estimating_them() -> None:
    trace = CacheLayoutTrace(
        step=0,
        provider="deepseek-v4-flash",
        request_fingerprint="a" * 64,
        primary_reason=CacheLayoutReason.COLD_START,
        reasons=[CacheLayoutReason.COLD_START],
        cache_hit_tokens=8,
        cache_miss_tokens=4,
    )
    events = [
        Event(type="cache.layout", task_id="cache-task", data=trace.model_dump(mode="json")),
        Event(
            type="model.completed",
            task_id="cache-task",
            data={
                "step": 0,
                "usage": ModelUsage(
                    input_tokens=12,
                    cache_hit_tokens=8,
                    cache_miss_tokens=4,
                ).model_dump(),
            },
        ),
    ]

    run = RealProviderCacheCollector.run_from_events(events)
    assert run.source == "provider_reported"
    assert run.input_tokens == 12
    assert run.cache_hit_tokens == 8
    assert run.cache_miss_tokens == 4
    assert run.steps[0].latency_ms is None


def test_provider_cache_controls_are_capability_gated() -> None:
    deepseek = provider_cache_capability("deepseek-v4-flash")
    assert deepseek.explicit_breakpoints is False
    assert (
        serialize_cache_controls(
            "deepseek-v4-flash", prompt_cache_key="session", breakpoints=["system"]
        )
        == {}
    )
    assert serialize_cache_controls(
        "openai", prompt_cache_key="session", breakpoints=["system"]
    ) == {"prompt_cache_key": "session", "cache_control": {"breakpoints": ["system"]}}


def test_benchmark_cache_cli_writes_machine_report_and_prints_summary(tmp_path: Path) -> None:
    output = tmp_path / "cache-matrix.json"
    result = runner.invoke(app, ["benchmark-cache", "--output", str(output)])

    assert result.exit_code == 0, result.output
    assert "PCO-06 cache matrix" in result.output
    assert output.is_file()
    assert '"schema_version": "pco-06.v1"' in output.read_text(encoding="utf-8")
