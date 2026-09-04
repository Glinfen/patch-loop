from __future__ import annotations

import json

import pytest

from patchloop.domain import PromptCacheLayout
from patchloop.prompt_cache import (
    MEMORY_SNAPSHOT_PREFIX,
    CacheEpochBoundary,
    PromptCacheCoordinator,
    PromptCacheCoordinatorError,
)
from patchloop.providers import ModelMessage, ModelUsage, ToolSpec


def _messages() -> list[ModelMessage]:
    return [
        ModelMessage(role="system", content="system instructions"),
        ModelMessage(role="user", content="task goal"),
        ModelMessage(role="assistant", content="initial inspection"),
    ]


def _tools() -> list[ToolSpec]:
    return [
        ToolSpec(name="read_file", description="read", parameters={"type": "object"}),
        ToolSpec(name="list_files", description="list", parameters={"type": "object"}),
    ]


def _projection(facts: list[dict[str, object]]) -> str:
    return "PATCHLOOP_PROVIDER_MEMORY_V1\nnotice\n" + json.dumps(
        {"facts": facts}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _usage() -> ModelUsage:
    return ModelUsage(
        input_tokens=100,
        output_tokens=8,
        cost_usd=0.01,
        cache_hit_tokens=75,
        cache_miss_tokens=25,
    )


def test_bootstrap_prepares_frozen_provider_request_and_records_response() -> None:
    tools = _tools()
    coordinator = PromptCacheCoordinator.start(_messages(), tools, layout=PromptCacheLayout.LEGACY)
    tools[0].description = "mutated after bootstrap"

    prepared = coordinator.prepare_request(
        0,
        [*_messages(), ModelMessage(role="user", content="continue")],
        provider="fake",
        model="model-1",
        thinking={"enabled": True},
    )

    assert prepared.epoch_id == "initial"
    assert prepared.cache_layout.primary_reason.value == "cold_start"
    assert prepared.tools[0].description == "read"
    assert [message.content for message in prepared.messages][-1] == "continue"

    observation = coordinator.observe_response(prepared, _usage())

    assert observation.cache_layout.cache_hit_tokens == 75
    assert observation.cache_layout.cache_miss_tokens == 25
    assert observation.cache_usage["cache_hit_tokens"] == 75
    assert observation.cache_usage["cache_usage_reported_calls"] == 1


def test_stable_publication_and_snapshot_restore_keep_next_request_identity() -> None:
    messages = _messages()
    coordinator = PromptCacheCoordinator.bootstrap(
        messages,
        _tools(),
        layout=PromptCacheLayout.STABLE,
        prefix_message_count=2,
    )
    projection = _projection([{"scope": "task", "text": "preserve API", "type": "fact"}])
    prepared = coordinator.prepare_request(
        0,
        messages,
        provider="fake",
        model="model-1",
        memory_projection=projection,
    )
    assert prepared.messages[2].content.startswith(MEMORY_SNAPSHOT_PREFIX)
    coordinator.observe_response(prepared, _usage())

    restored = PromptCacheCoordinator.from_snapshot(coordinator.snapshot())
    next_messages = [*messages, ModelMessage(role="user", content="run tests")]
    expected = coordinator.prepare_request(1, next_messages, provider="fake", model="model-1")
    actual = restored.prepare_request(1, next_messages, provider="fake", model="model-1")

    assert actual.epoch_id == expected.epoch_id == "initial"
    assert actual.cache_layout.request_fingerprint == expected.cache_layout.request_fingerprint
    assert actual.cache_layout.section_fingerprints == expected.cache_layout.section_fingerprints
    assert [message.model_dump() for message in actual.messages] == [
        message.model_dump() for message in expected.messages
    ]
    assert restored.snapshot().cache_usage == coordinator.snapshot().cache_usage


def test_compression_requires_observation_before_rollover_and_changes_epoch() -> None:
    coordinator = PromptCacheCoordinator.bootstrap(
        _messages(), _tools(), layout=PromptCacheLayout.STABLE, prefix_message_count=2
    )
    boundary = CacheEpochBoundary.CONTEXT_THRESHOLD
    prepared = coordinator.prepare_compression(
        1,
        [*_messages(), ModelMessage(role="user", content="more history")],
        boundary=boundary,
        provider="fake",
        model="model-1",
    )

    assert prepared.epoch_id == "initial"
    assert prepared.request.boundary is boundary
    assert prepared.request.messages[-1].content.startswith("PATCHLOOP_EPOCH_COMPRESSION_V1")
    with pytest.raises(PromptCacheCoordinatorError, match="observed compression"):
        coordinator.complete_compression(prepared, '{"next_step":"run tests"}')

    coordinator.observe_compression_response(prepared, _usage())
    snapshot = coordinator.complete_compression(prepared, '{"next_step":"run tests"}')

    assert snapshot.epoch_id == "initial.g1"
    assert snapshot.generation == 1
    assert snapshot.last_boundary is boundary
    assert coordinator.epoch_id == "initial.g1"

    with pytest.raises(PromptCacheCoordinatorError, match="different epoch"):
        coordinator.complete_compression(prepared, '{"next_step":"run tests again"}')


def test_legacy_layout_rejects_compression_and_rollover() -> None:
    coordinator = PromptCacheCoordinator.bootstrap(
        _messages(), _tools(), layout=PromptCacheLayout.LEGACY
    )
    with pytest.raises(PromptCacheCoordinatorError, match="stable layout"):
        coordinator.prepare_compression(
            0,
            _messages(),
            boundary=CacheEpochBoundary.EXPLICIT_COMPRESSION,
            provider="fake",
        )
    with pytest.raises(PromptCacheCoordinatorError, match="stable layout"):
        coordinator.rollover("summary", boundary=CacheEpochBoundary.EXPLICIT_COMPRESSION)


def test_pending_request_blocks_interleaving_and_mismatched_response() -> None:
    coordinator = PromptCacheCoordinator.bootstrap(
        _messages(), _tools(), layout=PromptCacheLayout.LEGACY
    )
    prepared = coordinator.prepare_request(0, _messages(), provider="fake")

    with pytest.raises(PromptCacheCoordinatorError, match="response is pending"):
        coordinator.prepare_request(1, _messages(), provider="fake")
    mismatched = prepared.model_copy(
        update={
            "cache_layout": prepared.cache_layout.model_copy(
                update={"request_fingerprint": "0" * 64}
            )
        }
    )
    with pytest.raises(PromptCacheCoordinatorError, match="does not match"):
        coordinator.observe_response(mismatched, _usage())
