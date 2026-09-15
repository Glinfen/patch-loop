from pathlib import Path

import pytest
from typer.testing import CliRunner

from patchloop.cli import app
from patchloop.domain import PromptCacheLayout
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


def test_runtime_fixture_benchmark_records_real_six_round_runtime_requests(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    report = CacheBenchmarkRunner().run_runtime_fixture(
        repository,
        layout=PromptCacheLayout.STABLE,
    )

    assert report.source == "deterministic"
    assert len(report.steps) == 7
    assert report.variant.value == "stable_prefix"
    assert report.steps[0].scenario.value == "cold_start"
    assert report.steps[-1].input_tokens is not None


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


def test_provider_collector_joins_compression_usage_by_request_and_deduplicates_attempts():
    events = []
    for index, kind in enumerate(("ordinary", "compression")):
        request_id = f"request-{index}"
        usage = ModelUsage(
            input_tokens=100 * (index + 1),
            output_tokens=10,
            cache_hit_tokens=80 * (index + 1),
            cache_miss_tokens=20 * (index + 1),
            cost_usd=0.1 * (index + 1),
            cost_status="estimated",
            input_tokens_reported=True,
            output_tokens_reported=True,
        )
        trace = CacheLayoutTrace(
            step=1,
            request_id=request_id,
            provider="real-test",
            request_fingerprint="a" * 64,
            comparison_kind=kind,
            metric_basis="normalized_messages_v1",
            previous_request_is_prefix=True,
            tools_unchanged=True,
            binding_unchanged=True,
            cache_hit_tokens=usage.cache_hit_tokens,
            cache_miss_tokens=usage.cache_miss_tokens,
            cache_usage_consistent=True,
        )
        events.extend(
            [
                Event(
                    type="provider.request.started",
                    task_id="task",
                    data={
                        "request_id": request_id,
                        "model": "m",
                        "binding_fingerprint": "b" * 64,
                        "endpoint_fingerprint": "c" * 64,
                        "input_budget": 8000,
                        "pricing_version": "v1",
                    },
                ),
                Event(
                    type="provider.attempt.started",
                    task_id="task",
                    data={
                        "request_id": request_id,
                        "attempt_id": f"attempt-{index}",
                    },
                ),
                Event(
                    type="provider.request.completed",
                    task_id="task",
                    data={
                        "request_id": request_id,
                        "attempt_id": f"attempt-{index}",
                        "usage": usage.model_dump(mode="json"),
                    },
                ),
                Event(type="cache.layout", task_id="task", data=trace.model_dump(mode="json")),
            ]
        )
    events.extend(events[-2:])
    run = RealProviderCacheCollector.run_from_events(events)
    assert [s.input_tokens for s in run.steps] == [100, 200]
    assert run.input_tokens == 300
    assert run.cache_hit_tokens == 240
    assert run.compression_count == 1
    assert run.cost_usd == pytest.approx(0.3)
    assert run.request_linkage_complete
    assert run.unknown_usage_attempts == 0
    resumed = RealProviderCacheCollector.run_from_events(
        [Event(type="task.resumed", task_id="task", data={}), *events]
    )
    assert resumed.restored_request_count == 1
    assert resumed.steps[0].restored
    events.append(
        Event(
            type="provider.attempt.started",
            task_id="task",
            data={
                "request_id": "request-0",
                "attempt_id": "unknown-attempt",
            },
        )
    )
    unknown = RealProviderCacheCollector.run_from_events(events)
    assert unknown.unknown_usage_attempts == 1
    assert unknown.cost_usd is None
    incomplete = RealProviderCacheCollector.run_from_events(
        [
            e
            for e in events
            if not (e.type == "provider.request.completed" and e.data["request_id"] == "request-1")
        ]
    )
    assert incomplete.input_tokens is None
    assert not incomplete.request_linkage_complete
