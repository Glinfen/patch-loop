from __future__ import annotations

import json
from pathlib import Path

from patchloop.domain import PromptCacheLayout, Task, TaskBudget, TaskExecutionConfig, ToolCall
from patchloop.evaluation import CacheBenchmarkRunner
from patchloop.events import EventLogger
from patchloop.persistence import RuntimeCheckpoint, SQLiteStore
from patchloop.prompt_cache import (
    MEMORY_DELTA_PREFIX,
    MEMORY_SNAPSHOT_PREFIX,
    PROJECT_INSTRUCTIONS_PREFIX,
    SUMMARY_PREFIX,
    CacheDiagnostics,
    CacheEpoch,
    CacheEpochBoundary,
    CacheLayoutReason,
    MemoryDeltaPublisher,
    PromptLayout,
    fingerprint_request,
)
from patchloop.providers import FakeProvider, ModelMessage, ModelResponse, ModelUsage, ToolSpec
from patchloop.runtime import SYSTEM_PROMPT, AgentRuntime
from patchloop.tools import ListFilesTool, ReadFileTool, ToolContext, ToolGateway


def _messages() -> list[ModelMessage]:
    return [
        ModelMessage(role="system", content="system"),
        ModelMessage(role="user", content="goal"),
        ModelMessage(role="assistant", content="answer"),
    ]


def _tools() -> list[ToolSpec]:
    return [
        ToolSpec(name="zeta", description="z", parameters={"type": "object"}),
        ToolSpec(name="alpha", description="a", parameters={"type": "object"}),
    ]


def _projection(payload: dict[str, object]) -> str:
    return "PATCHLOOP_PROVIDER_MEMORY_V1\nnotice\n" + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def test_request_wire_and_section_fingerprints_are_the_current_contract() -> None:
    fingerprint, wire = fingerprint_request(
        _messages(),
        _tools(),
        model="model-1",
        thinking={"enabled": True},
        epoch_snapshot={"id": "epoch-1"},
        system_instructions="system",
        task_project_snapshot={"files": ["a.py"], "goal": "goal"},
        memory_projection={"state": "new"},
    )
    assert fingerprint.model_dump_json(
        exclude={
            "message_fingerprints",
            "message_estimated_tokens",
            "ordered_tools_fingerprint",
            "binding_fingerprint",
        }
    ) == (
        '{"system_instructions":{"fingerprint":"0dc8cd0a92280156c12ba5073f5049586727ba0bef2ff72099577bd332db18f5",'
        '"byte_length":8,"estimated_tokens":3},"task_project_snapshot":{"fingerprint":"6ac9d1230147cdfafbbd0f2603e612b48aeda83b037bbb6351a49698cf63f41c",'
        '"byte_length":32,"estimated_tokens":11},"tool_schema":{"fingerprint":"088d0ee1d150c5ba508883c0ebf7572d50509a9a61774891663c3d7f90ffae88",'
        '"byte_length":172,"estimated_tokens":58},"epoch_snapshot":{"fingerprint":"cb754ad7bb8672e4f362a560321c2fb498e33ba1a2e3bb8bd5401429a4c1c87a",'
        '"byte_length":16,"estimated_tokens":6},"memory_projection":{"fingerprint":"3e72db8d0d44c0ad58f16576f902c91b5f797c6f08209bae880175092458b809",'
        '"byte_length":15,"estimated_tokens":5},"history_groups":{"fingerprint":"438ce820fc507821e7d8efbbea6a7996d058c1636ffba221014da945d76f8652",'
        '"byte_length":79,"estimated_tokens":27},"request":{"fingerprint":"e6af82ce52a40ee01ecacc1179bd100fdb5d3984e78571b3da00ed92c590611e",'
        '"byte_length":459,"estimated_tokens":153},"dynamic_system_prefix":{"fingerprint":"0dc8cd0a92280156c12ba5073f5049586727ba0bef2ff72099577bd332db18f5",'
        '"byte_length":8,"estimated_tokens":3},"history_group_fingerprints":["df1bac7e7f5e376d3994397e009f8ca2baec19ff16bc2f8220d70d9edcc7da70"],'
        '"provider_fingerprint":"2060227fb9ca2a7b4ada03f4737511f252908edb2252337586ae7e63d748b8ab",'
        '"epoch_fingerprint":"cb754ad7bb8672e4f362a560321c2fb498e33ba1a2e3bb8bd5401429a4c1c87a",'
        '"request_wire_fingerprint":"e6af82ce52a40ee01ecacc1179bd100fdb5d3984e78571b3da00ed92c590611e"}'
    )
    assert wire.decode("utf-8") == (
        '{"messages":[{"content":"system","role":"system","tool_call_id":null,"tool_calls":[]},'
        '{"content":"goal","role":"user","tool_call_id":null,"tool_calls":[]},'
        '{"content":"answer","role":"assistant","tool_call_id":null,"tool_calls":[]}],'
        '"model":"model-1","thinking":{"enabled":true},"tools":[{"description":"a","name":"alpha",'
        '"parameters":{"type":"object"},"permission":"read"},{"description":"z","name":"zeta",'
        '"parameters":{"type":"object"},"permission":"read"}]}'
    )


def test_deterministic_cache_matrix_matches_the_pcr00_baseline() -> None:
    baseline_path = (
        Path(__file__).parents[2] / "benchmarks" / "results" / "pcr00_cache_baseline.json"
    )
    expected = json.loads(baseline_path.read_text(encoding="utf-8"))

    actual = CacheBenchmarkRunner(repeats=3).run().model_dump(mode="json")

    # PPS adds diagnostic fields. Keep every historical value under regression,
    # while the report digest now also covers the new fields.
    def legacy_projection(value: object, template: object) -> object:
        if isinstance(template, dict) and isinstance(value, dict):
            return {
                key: legacy_projection(value[key], item)
                for key, item in template.items()
                if key != "deterministic_fingerprint"
            }
        if isinstance(template, list) and isinstance(value, list):
            assert len(value) == len(template)
            return [
                legacy_projection(item, shape) for item, shape in zip(value, template, strict=True)
            ]
        return value

    assert legacy_projection(actual, expected) == legacy_projection(expected, expected)
    assert actual["deterministic_fingerprint"] == (
        CacheBenchmarkRunner(repeats=3).run().deterministic_fingerprint
    )


def test_legacy_and_stable_layouts_keep_roles_order_and_normalized_content() -> None:
    legacy = PromptLayout(PromptCacheLayout.LEGACY).initial_messages(
        SYSTEM_PROMPT,
        "Inspect repository",
        project_instructions="Keep the change narrow.",
    )
    stable = PromptLayout(PromptCacheLayout.STABLE).initial_messages(
        SYSTEM_PROMPT,
        "Inspect repository",
        project_instructions="Keep the change narrow.",
    )

    assert [(message.role, message.content) for message in legacy] == [
        ("system", SYSTEM_PROMPT),
        ("user", "Inspect repository"),
    ]
    assert [(message.role, message.content) for message in stable] == [
        ("system", SYSTEM_PROMPT),
        ("system", PROJECT_INSTRUCTIONS_PREFIX + "Keep the change narrow."),
        ("user", "Inspect repository"),
    ]


def test_epoch_and_publication_snapshots_keep_checkpoint_json_shape() -> None:
    messages = [
        ModelMessage(role="system", content="static instructions"),
        ModelMessage(role="user", content="repair the parser"),
        ModelMessage(role="assistant", content="I inspected parser.py"),
        ModelMessage(role="tool", content="test failed", tool_call_id="call-1"),
    ]
    epoch = CacheEpoch.bootstrap(messages, prefix_message_count=2, epoch_id="initial")
    next_epoch = epoch.rollover(
        json.dumps({"constraints": ["keep API"], "next_step": "run tests"}),
        boundary=CacheEpochBoundary.CONTEXT_THRESHOLD,
    )
    expected_epoch = epoch.snapshot.model_dump(mode="json")
    expected_next_epoch = next_epoch.snapshot.model_dump(mode="json")

    assert expected_epoch["prefix_fingerprint"] == (
        "d820fd364b86d59e663a03d28b4dc2a8bf3ba2e1764799aa1ca151c9fc901e54"
    )
    assert expected_next_epoch["prefix_fingerprint"] == (
        "0cf25a8fd2c23004bde61a442f2fa7c9127b7f4b120bb7af436c1aa5431e78b5"
    )
    assert expected_next_epoch["prefix_messages"][-1]["content"] == (
        SUMMARY_PREFIX + '{"constraints":["keep API"],"next_step":"run tests"}'
    )

    payload = {"facts": [{"scope": "repository", "text": "MODE is stable", "type": "code_symbol"}]}
    publisher, snapshot_message = MemoryDeltaPublisher().publish("epoch-1", _projection(payload))
    payload["facts"].append({"scope": "task", "text": "run tests", "type": "verification"})
    publisher, delta_message = publisher.publish("epoch-1", _projection(payload))

    assert snapshot_message is not None and snapshot_message.content.startswith(
        MEMORY_SNAPSHOT_PREFIX
    )
    assert delta_message is not None and delta_message.content.startswith(MEMORY_DELTA_PREFIX)
    assert publisher.snapshot is not None
    assert publisher.snapshot.snapshot_fingerprint == (
        "e5714b44cbbd52f12d8d93743af032b281617e7a45a6de15930f1da837c6f325"
    )
    assert publisher.snapshot.current_fingerprint == (
        "c1c5dded76146eb82ecd07b52f558ca946702c554764bbab59dd74a6454dcf0c"
    )
    assert publisher.snapshot.delta_count == 1
    assert MemoryDeltaPublisher.replay(publisher.snapshot) == payload


def test_checkpoint_restore_preserves_the_next_request_fingerprint(tmp_path: Path) -> None:
    first = _messages()
    second = [*first, ModelMessage(role="user", content="continue")]
    tools = _tools()
    diagnostics = CacheDiagnostics()
    diagnostics.observe(0, first, tools, provider="fake", model="model-1")
    checkpoint = RuntimeCheckpoint(
        task_id="task-1",
        next_step_index=1,
        messages=first,
        cache_diagnostics=diagnostics.snapshot(),
    )
    store = SQLiteStore(tmp_path / "state" / "patchloop.db")
    task = Task(id="task-1", goal="goal", repository=str(tmp_path))
    store.save_task(task)
    store.save_checkpoint(checkpoint)

    restored = CacheDiagnostics()
    restored.restore(store.get_checkpoint(task.id).cache_diagnostics)
    restored_trace = restored.observe(1, second, tools, provider="fake", model="model-1")

    uninterrupted = CacheDiagnostics()
    uninterrupted.observe(0, first, tools, provider="fake", model="model-1")
    uninterrupted_trace = uninterrupted.observe(1, second, tools, provider="fake", model="model-1")
    assert restored_trace.request_fingerprint == uninterrupted_trace.request_fingerprint
    assert restored_trace.section_fingerprints == uninterrupted_trace.section_fingerprints
    assert restored_trace.provider_fingerprint == uninterrupted_trace.provider_fingerprint
    assert restored_trace.epoch_fingerprint == uninterrupted_trace.epoch_fingerprint


def test_runtime_step_trace_and_report_fields_are_stable(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    trace = EventLogger(tmp_path / "trace.jsonl")
    provider = FakeProvider(
        [
            ModelResponse(
                content="done",
                usage=ModelUsage(
                    input_tokens=100,
                    output_tokens=7,
                    cost_usd=0.01,
                    cache_hit_tokens=80,
                    cache_miss_tokens=20,
                    cache_write_tokens=5,
                ),
            )
        ]
    )
    gateway = ToolGateway(ToolContext(repository), [ListFilesTool()], trace)

    result = AgentRuntime(provider, gateway, trace).run(
        Task(goal="inspect", repository=str(repository))
    )

    assert result.report is not None
    report = result.report.model_dump(mode="json")
    assert {
        key: report[key]
        for key in {
            "summary",
            "changed_files",
            "diff",
            "validations",
            "tool_calls",
            "successful_tool_calls",
            "failed_tool_calls",
            "replans",
            "input_tokens",
            "output_tokens",
            "cost_usd",
            "cache_hit_tokens",
            "cache_miss_tokens",
            "cache_write_tokens",
            "cache_hit_rate",
            "cache_usage_reported_calls",
            "cache_usage_unreported_calls",
            "cache_usage_inconsistent_calls",
            "cache_write_reported_calls",
        }
    } == {
        "summary": "done",
        "changed_files": [],
        "diff": "",
        "validations": [],
        "tool_calls": 0,
        "successful_tool_calls": 0,
        "failed_tool_calls": 0,
        "replans": 0,
        "input_tokens": 100,
        "output_tokens": 7,
        "cost_usd": 0.01,
        "cache_hit_tokens": 80,
        "cache_miss_tokens": 20,
        "cache_write_tokens": 5,
        "cache_hit_rate": 0.8,
        "cache_usage_reported_calls": 1,
        "cache_usage_unreported_calls": 0,
        "cache_usage_inconsistent_calls": 0,
        "cache_write_reported_calls": 1,
    }
    events = trace.read()
    assert [
        event.type for event in events if event.type in {"cache.layout", "model.completed"}
    ] == [
        "cache.layout",
        "model.completed",
    ]
    layout = next(event for event in events if event.type == "cache.layout")
    assert layout.data["step"] == 0
    assert layout.data["primary_reason"] == CacheLayoutReason.COLD_START.value
    assert set(layout.data) == {
        "request_id",
        "source_request_id",
        "message_count",
        "previous_message_count",
        "common_prefix_message_count",
        "previous_request_is_prefix",
        "first_changed_message_index",
        "common_prefix_estimated_tokens",
        "tools_unchanged",
        "binding_unchanged",
        "comparison_kind",
        "prefix_break_reason",
        "metric_basis",
        "legacy_lcp_basis",
        "step",
        "provider",
        "request_fingerprint",
        "provider_fingerprint",
        "epoch_fingerprint",
        "section_fingerprints",
        "section_byte_lengths",
        "stable_prefix_bytes",
        "stable_prefix_tokens",
        "longest_common_prefix_bytes",
        "longest_common_prefix_tokens",
        "first_change_section",
        "reasons",
        "primary_reason",
        "secondary_reasons",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "cache_usage_consistent",
        "provider_best_effort",
    }


def test_runtime_usage_report_preserves_missing_zero_and_inconsistent_fields(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (repository / name).write_text(f"{name}=1\n", encoding="utf-8")
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="read-a", name="read_file", arguments={"path": "a.py"})],
                usage=ModelUsage(
                    input_tokens=10,
                    cache_hit_tokens=6,
                    cache_miss_tokens=4,
                    cache_write_tokens=3,
                ),
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="read-b", name="read_file", arguments={"path": "b.py"})],
                usage=ModelUsage(input_tokens=20),
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="read-c", name="read_file", arguments={"path": "c.py"})],
                usage=ModelUsage(input_tokens=30, cache_hit_tokens=10, cache_miss_tokens=25),
            ),
            ModelResponse(
                content="done",
                usage=ModelUsage(
                    input_tokens=40,
                    cache_hit_tokens=0,
                    cache_miss_tokens=40,
                    cache_write_tokens=0,
                ),
            ),
        ]
    )
    gateway = ToolGateway(
        ToolContext(repository), [ReadFileTool()], EventLogger(tmp_path / "trace.jsonl")
    )

    result = AgentRuntime(provider, gateway).run(Task(goal="inspect", repository=str(repository)))

    assert result.report is not None
    assert result.report.input_tokens == 100
    assert result.report.cache_hit_tokens == 16
    assert result.report.cache_miss_tokens == 69
    assert result.report.cache_hit_rate == 16 / 85
    assert result.report.cache_write_tokens == 3
    assert result.report.cache_usage_reported_calls == 3
    assert result.report.cache_usage_unreported_calls == 1
    assert result.report.cache_usage_inconsistent_calls == 1
    assert result.report.cache_write_reported_calls == 2


class _CompressionProvider:
    def __init__(self) -> None:
        self.responses = iter(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="read-early",
                            name="read_file",
                            arguments={"path": "early.py", "max_chars": 20_000},
                        )
                    ],
                    usage=ModelUsage(input_tokens=10, cache_hit_tokens=6, cache_miss_tokens=4),
                ),
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="read-noise",
                            name="read_file",
                            arguments={"path": "noise.py", "max_chars": 20_000},
                        )
                    ],
                    usage=ModelUsage(input_tokens=20, cache_hit_tokens=10, cache_miss_tokens=10),
                ),
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="read-noise-2",
                            name="read_file",
                            arguments={"path": "noise-2.py", "max_chars": 20_000},
                        )
                    ],
                    usage=ModelUsage(input_tokens=30, cache_hit_tokens=15, cache_miss_tokens=15),
                ),
                ModelResponse(
                    content='{"constraints":["keep API"],"next_step":"finish"}',
                    usage=ModelUsage(input_tokens=40, cache_hit_tokens=20, cache_miss_tokens=20),
                ),
                ModelResponse(
                    content="done",
                    usage=ModelUsage(input_tokens=50, cache_hit_tokens=25, cache_miss_tokens=25),
                ),
            ]
        )

    @property
    def name(self) -> str:
        return "compression-fake"

    def complete(
        self,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        del messages, tools
        return next(self.responses)


def test_runtime_epoch_compression_keeps_trace_events_and_report_fields(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "early.py").write_text(
        "EARLY=1\n" + "early_noise = 1\n" * 1_000, encoding="utf-8"
    )
    (repository / "noise.py").write_text("noise = 'x'\n" * 4_000, encoding="utf-8")
    (repository / "noise-2.py").write_text("noise = 'y'\n" * 4_000, encoding="utf-8")
    trace = EventLogger(tmp_path / "trace.jsonl")
    gateway = ToolGateway(ToolContext(repository), [ReadFileTool()], trace)
    task = Task(
        goal="inspect the repository",
        repository=str(repository),
        execution=TaskExecutionConfig(prompt_cache_layout=PromptCacheLayout.STABLE),
        budget=TaskBudget(max_steps=8, max_context_tokens=2_000, max_tool_output_chars=400),
    )

    result = AgentRuntime(_CompressionProvider(), gateway, trace).run(task)

    assert result.result == "done"
    assert result.report is not None
    assert result.report.input_tokens == 150
    assert result.report.cache_hit_tokens == 76
    assert result.report.cache_miss_tokens == 74
    assert result.report.cache_hit_rate == 76 / 150
    assert result.report.cache_usage_reported_calls == 5
    events = trace.read()
    compression_requested = [
        event for event in events if event.type == "cache.compression.requested"
    ]
    rolled_over = [event for event in events if event.type == "cache.epoch.rolled_over"]
    layout_events = [event for event in events if event.type == "cache.layout"]
    assert len(compression_requested) == 1
    assert compression_requested[0].data["epoch_id"] == "initial"
    assert compression_requested[0].data["generation"] == 0
    assert compression_requested[0].data["boundary"] == "context_threshold"
    assert len(rolled_over) == 1
    assert rolled_over[0].data["old_epoch_id"] == "initial"
    assert rolled_over[0].data["new_epoch_id"] == "initial.g1"
    assert len(layout_events) == 5
    assert [event.data["step"] for event in layout_events] == [0, 1, 2, 2, 3]
    assert result.report.context_compactions > 0
