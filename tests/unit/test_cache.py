from patchloop.domain import Task
from patchloop.events import EventLogger
from patchloop.observability import TaskMetrics, TaskReplay
from patchloop.prompt_cache import (
    CacheDiagnostics,
    CacheLayoutReason,
    fingerprint_request,
    fingerprint_text,
)
from patchloop.providers import FakeProvider, ModelMessage, ModelResponse, ModelUsage, ToolSpec
from patchloop.runtime import AgentRuntime
from patchloop.tools import ListFilesTool, ToolContext, ToolGateway


def _tools() -> list[ToolSpec]:
    return [
        ToolSpec(name="zeta", description="z", parameters={"type": "object"}),
        ToolSpec(name="alpha", description="a", parameters={"type": "object"}),
    ]


def _messages(
    *,
    system: str = "system",
    project: str = "goal",
    history: list[ModelMessage] | None = None,
) -> list[ModelMessage]:
    return [
        ModelMessage(role="system", content=system),
        ModelMessage(role="user", content=project),
        *(history or []),
    ]


def test_request_fingerprints_are_canonical_and_secret_safe() -> None:
    first, _ = fingerprint_request(
        _messages(),
        _tools(),
        model="model-1",
        thinking={"enabled": True},
        system_instructions="system",
        task_project_snapshot={"goal": "goal", "files": ["a.py"]},
    )
    second, _ = fingerprint_request(
        _messages(),
        list(reversed(_tools())),
        model="model-1",
        thinking={"enabled": True},
        system_instructions="system",
        task_project_snapshot={"files": ["a.py"], "goal": "goal"},
    )

    assert first.tool_schema_fingerprint == second.tool_schema_fingerprint
    assert first.project_fingerprint == second.project_fingerprint
    assert first.request_wire_fingerprint == second.request_wire_fingerprint
    assert fingerprint_text("Authorization: Bearer very-secret-token") == fingerprint_text(
        "Authorization: Bearer [REDACTED]"
    )
    serialized = first.model_dump_json()
    assert "very-secret-token" not in serialized


def test_cache_diagnostics_attributes_structural_changes_and_large_provider_misses() -> None:
    diagnostics = CacheDiagnostics(miss_threshold_tokens=100)
    first = diagnostics.observe(0, _messages(), _tools(), provider="fake", model="m")
    assert first.primary_reason is CacheLayoutReason.COLD_START

    memory = diagnostics.observe(
        1,
        _messages(history=[ModelMessage(role="assistant", content="answer")]),
        _tools(),
        provider="fake",
        model="m",
        memory_projection={"state": "new"},
    )
    memory = diagnostics.finalize(
        memory,
        ModelUsage(input_tokens=120, cache_hit_tokens=20, cache_miss_tokens=100),
    )
    assert memory.primary_reason is CacheLayoutReason.MEMORY_PROJECTION_CHANGE
    assert memory.cache_usage_consistent is True
    assert memory.longest_common_prefix_bytes > 0

    provider = diagnostics.observe(
        2,
        _messages(history=[ModelMessage(role="assistant", content="answer")]),
        _tools(),
        provider="fake",
        model="m",
        memory_projection={"state": "new"},
    )
    provider = diagnostics.finalize(
        provider,
        ModelUsage(input_tokens=101, cache_hit_tokens=0, cache_miss_tokens=101),
    )
    assert provider.primary_reason is CacheLayoutReason.PROVIDER_BEST_EFFORT
    assert provider.provider_best_effort is True


def test_cache_diagnostics_detects_history_reselection_and_model_change() -> None:
    diagnostics = CacheDiagnostics()
    diagnostics.observe(
        0,
        _messages(
            history=[
                ModelMessage(role="assistant", content="first"),
                ModelMessage(role="tool", content="one", tool_call_id="c1"),
                ModelMessage(role="assistant", content="second"),
            ]
        ),
        _tools(),
        provider="fake",
        model="m",
    )
    reselection = diagnostics.observe(
        1,
        _messages(
            history=[
                ModelMessage(role="assistant", content="second"),
                ModelMessage(role="tool", content="one", tool_call_id="c1"),
            ]
        ),
        _tools(),
        provider="fake",
        model="m",
    )
    assert reselection.primary_reason is CacheLayoutReason.HISTORY_RESELECTION

    changed = diagnostics.observe(
        2,
        _messages(),
        _tools(),
        provider="fake",
        model="m-2",
        thinking={"enabled": False},
        epoch_snapshot={"id": "epoch-2"},
    )
    assert CacheLayoutReason.MODEL_OR_THINKING_CHANGE in changed.reasons
    assert CacheLayoutReason.EPOCH_ROLLOVER in changed.reasons


def test_runtime_emits_safe_layout_trace_consumable_by_metrics_and_replay(tmp_path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    trace = EventLogger(tmp_path / "trace.jsonl")
    provider = FakeProvider(
        [
            ModelResponse(
                content="done",
                usage=ModelUsage(
                    input_tokens=100,
                    cache_hit_tokens=80,
                    cache_miss_tokens=20,
                ),
            )
        ]
    )
    gateway = ToolGateway(ToolContext(repository), [ListFilesTool()], trace)

    result = AgentRuntime(provider, gateway, trace).run(
        Task(goal="inspect", repository=str(repository))
    )

    assert result.report is not None
    events = trace.read()
    layout_events = [event for event in events if event.type == "cache.layout"]
    assert len(layout_events) == 1
    assert layout_events[0].data["primary_reason"] == "cold_start"
    assert "inspect" not in layout_events[0].model_dump_json()
    metrics = TaskMetrics.from_events(result.id, events)
    replay = TaskReplay.from_events(result.id, events)
    assert metrics.cache_layout_events == 1
    assert metrics.cache_layout_primary_reasons == {"cold_start": 1}
    assert replay.cache_layouts[0].cache_hit_tokens == 80
