from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from patchloop.context import ContextEngine
from patchloop.domain import PromptCacheLayout, TaskBudget, ToolCall
from patchloop.prompt_cache import (
    COMPRESSION_INSTRUCTION,
    MEMORY_SNAPSHOT_PREFIX,
    AppendOnlyOptimizationPolicy,
    AppendOnlyPromptState,
    CacheEpoch,
    CacheEpochBoundary,
    CompressionDecision,
    CompressionFailureAction,
    MemoryDeltaPublisher,
    MemoryPublicationSnapshot,
    PrefixBudget,
    PromptCacheCoordinator,
    PromptCacheCoordinatorError,
    PromptCacheCoordinatorSnapshot,
    PromptCompressionRejected,
    compute_prefix_budget,
    decide_append_only_compression,
    estimate_append_only_mandatory_rebase_tokens,
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


def _strict_summary(next_step: str = "run tests") -> str:
    return json.dumps(
        {
            "constraints": ["preserve the contract"],
            "paths": [],
            "decisions": [],
            "failures": [],
            "tests": [],
            "unfinished": ["verify output"],
            "next_step": next_step,
        },
        ensure_ascii=False,
    )


def _append_only_compression_fixture() -> tuple[
    PromptCacheCoordinator,
    list[ModelMessage],
    list[ToolSpec],
    MemoryPublicationSnapshot,
]:
    root = [
        ModelMessage(role="system", content="static system"),
        ModelMessage(role="user", content="repair the parser"),
    ]
    tools = _tools()
    publisher = MemoryDeltaPublisher()
    first = publisher.preview(
        "initial",
        _projection([{"scope": "task", "text": "original memory", "type": "fact"}]),
        invalidated_values=[],
        max_message_tokens=2_048,
    )
    second = MemoryDeltaPublisher(first.next_state).preview(
        "initial",
        _projection(
            [
                {"scope": "task", "text": "original memory", "type": "fact"},
                {"scope": "task", "text": "second memory delta", "type": "fact"},
            ]
        ),
        invalidated_values=[],
        max_message_tokens=2_048,
    )
    epoch = CacheEpoch.bootstrap(
        root,
        prefix_message_count=2,
        root_prefix_message_count=2,
        epoch_id="initial",
    )
    coordinator = PromptCacheCoordinator(
        layout=PromptCacheLayout.APPEND_ONLY,
        cache_epoch_id="initial",
        prefix_message_count=2,
        frozen_tools=tools,
        cache_epoch=epoch,
        publication=MemoryDeltaPublisher(second.next_state),
        append_only_state=AppendOnlyPromptState(root_prefix_message_count=2),
    )
    source = [
        *root,
        *second.next_state.messages,
        ModelMessage(role="assistant", content="old history " + "x" * 20_000),
    ]
    prepared = coordinator.prepare_request(
        0,
        source,
        provider="fake",
        model="model-1",
        thinking={"enabled": False},
        request_id="source-request-1",
    )
    coordinator.observe_response(prepared, _usage())
    return coordinator, source, tools, second.next_state


def _compression_decision(
    coordinator: PromptCacheCoordinator,
    candidate: list[ModelMessage],
    unsent_suffix: list[ModelMessage],
    budget: PrefixBudget,
    *,
    step: int,
    boundary: CacheEpochBoundary,
    publication: MemoryPublicationSnapshot | None = None,
) -> CompressionDecision:
    policy = coordinator.optimization_policy
    state = coordinator.append_only_state
    assert policy is not None
    assert state is not None
    publication_state = publication or coordinator.publication_snapshot
    mandatory = estimate_append_only_mandatory_rebase_tokens(
        root_messages=coordinator.frozen_prefix[: state.root_prefix_message_count],
        unsent_suffix_messages=unsent_suffix,
        publication_state=publication_state,
        tools=coordinator.frozen_tools,
        memory_message_limit=budget.memory_message_limit,
    )
    return decide_append_only_compression(
        policy=policy,
        budget=budget,
        candidate_input_tokens=(
            ContextEngine.estimate_messages(candidate)
            + ContextEngine.estimate_tools(coordinator.frozen_tools)
        ),
        mandatory_rebase_tokens=mandatory,
        step=step,
        last_compression_attempt_step=state.last_compression_attempt_step,
        has_submitted_source=(
            state.last_submitted_message_count > 0
            and state.last_submitted_request_id is not None
        ),
        recovering_compression=state.compression_request_id is not None,
        oversized_delta=boundary is CacheEpochBoundary.EXPLICIT_COMPRESSION,
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


def test_append_only_bootstrap_creates_its_frozen_epoch_and_root_state() -> None:
    coordinator = PromptCacheCoordinator.bootstrap(
        _messages(),
        _tools(),
        layout=PromptCacheLayout.APPEND_ONLY,
        prefix_message_count=2,
    )

    assert coordinator.snapshot().cache_epoch_state is not None
    assert coordinator.append_only_state is not None
    assert coordinator.append_only_state.root_prefix_message_count == 2


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


def test_append_only_compression_reuses_submitted_source_and_preserves_unsent_suffix() -> None:
    coordinator, source, _, publication = _append_only_compression_fixture()
    call = ToolCall(id="call-unsent", name="read_file", arguments={"path": "new.py"})
    unsent_suffix = [
        ModelMessage(role="assistant", content="", tool_calls=[call]),
        ModelMessage(role="tool", content="new file contents", tool_call_id=call.id),
        ModelMessage(role="user", content="also inspect this new file"),
    ]
    candidate_publication = MemoryDeltaPublisher(publication).preview(
        "initial",
        _projection(
            [
                {"scope": "task", "text": "original memory", "type": "fact"},
                {"scope": "task", "text": "second memory delta", "type": "fact"},
                {"scope": "task", "text": "current memory candidate", "type": "fact"},
            ]
        ),
        invalidated_values=[],
        max_message_tokens=2_048,
    )
    candidate_messages = [*source, *unsent_suffix, *candidate_publication.messages]
    budget = PrefixBudget(
        input_limit=20_000,
        ordinary_limit=8_000,
        soft_limit=1_000,
        memory_message_limit=2_048,
        summary_limit=512,
    )
    boundary = CacheEpochBoundary.CONTEXT_THRESHOLD
    decision = _compression_decision(
        coordinator,
        candidate_messages,
        unsent_suffix,
        budget,
        step=1,
        boundary=boundary,
        publication=candidate_publication.next_state,
    )

    prepared = coordinator.prepare_compression(
        1,
        candidate_messages,
        boundary=boundary,
        provider="fake",
        model="model-1",
        thinking={"enabled": False},
        source_messages=source,
        unsent_suffix_messages=unsent_suffix,
        candidate_memory_messages=candidate_publication.messages,
        source_request_id="source-request-1",
        source_message_count=len(source),
        budget=budget,
        decision=decision,
        candidate_publication_state=candidate_publication.next_state,
    )

    assert prepared.request.messages[:-1] == source
    assert prepared.request.messages[-1].content.startswith("PATCHLOOP_EPOCH_COMPRESSION_V1")
    assert prepared.request.source_request_id == "source-request-1"
    assert prepared.request.source_message_count == len(source)
    assert prepared.unsent_suffix == unsent_suffix
    assert all(message not in prepared.request.messages for message in unsent_suffix)
    assert all(
        message not in prepared.request.messages for message in candidate_publication.messages
    )

    coordinator.observe_compression_response(prepared, _usage())
    completion = coordinator.complete_append_only_compression(prepared, _strict_summary())

    assert completion.epoch.generation == 1
    assert completion.epoch.prefix_message_count == 3
    assert completion.epoch.prefix_messages[:2] == source[:2]
    assert completion.messages[:3] == completion.epoch.prefix_messages
    assert completion.messages[3:-1] == unsent_suffix
    assert completion.messages[-1].content.startswith("PATCHLOOP_MEMORY_SNAPSHOT_V2")
    assert completion.publication_state.epoch_id == completion.epoch.epoch_id
    assert len(completion.publication_state.messages) == 1
    assert completion.candidate_input_tokens > completion.rebased_input_tokens
    assert completion.summary_estimated_tokens > 0
    assert completion.summary_target_tokens is None

    next_request = coordinator.prepare_request(
        2,
        completion.messages,
        provider="fake",
        model="model-1",
        thinking={"enabled": False},
        request_id="new-epoch-request-1",
    )
    assert next_request.messages == completion.messages
    assert coordinator.append_only_state is not None
    assert coordinator.append_only_state.last_submitted_epoch_generation == 1


@pytest.mark.parametrize(
    ("summary", "reason"),
    [(_strict_summary(next_step="x"), "no_gain"), ("not json", "invalid_summary")],
)
def test_append_only_compression_rejection_keeps_epoch_and_publication(
    summary: str,
    reason: str,
) -> None:
    root = [
        ModelMessage(role="system", content="static system"),
        ModelMessage(role="user", content="small task"),
    ]
    tools = _tools()
    coordinator = PromptCacheCoordinator.bootstrap(
        root,
        tools,
        layout=PromptCacheLayout.APPEND_ONLY,
        prefix_message_count=2,
        epoch_id="initial",
    )
    source_request = coordinator.prepare_request(
        0,
        root,
        provider="fake",
        model="model-1",
        thinking={"enabled": False},
        request_id="small-source",
    )
    coordinator.observe_response(source_request, _usage())
    budget = PrefixBudget(
        input_limit=20_000,
        ordinary_limit=10_000,
        soft_limit=1,
        memory_message_limit=2_048,
        summary_limit=512,
    )
    boundary = CacheEpochBoundary.EXPLICIT_COMPRESSION
    decision = _compression_decision(
        coordinator,
        root,
        [],
        budget,
        step=1,
        boundary=boundary,
    )
    prepared = coordinator.prepare_compression(
        1,
        root,
        boundary=boundary,
        provider="fake",
        model="model-1",
        thinking={"enabled": False},
        source_messages=root,
        unsent_suffix_messages=[],
        source_request_id="small-source",
        source_message_count=len(root),
        budget=budget,
        decision=decision,
    )
    old_epoch = coordinator.snapshot().cache_epoch_state
    old_publication = coordinator.publication_snapshot
    coordinator.observe_compression_response(prepared, _usage())

    with pytest.raises(PromptCompressionRejected) as error:
        coordinator.complete_append_only_compression(prepared, summary)

    assert error.value.reason == reason
    assert error.value.action is CompressionFailureAction.CONTINUE_OLD_EPOCH
    assert coordinator.snapshot().cache_epoch_state == old_epoch
    assert coordinator.publication_snapshot == old_publication
    assert coordinator.append_only_state is not None
    assert (
        coordinator.append_only_state.deferred_compression_fingerprint
        == prepared.source_fingerprint
    )
    with pytest.raises(PromptCompressionRejected, match="same_source_deferred"):
        retry_decision = _compression_decision(
            coordinator,
            root,
            [],
            budget,
            step=2,
            boundary=boundary,
        )
        coordinator.prepare_compression(
            2,
            root,
            boundary=boundary,
            provider="fake",
            model="model-1",
            thinking={"enabled": False},
            source_messages=root,
            unsent_suffix_messages=[],
            source_request_id="small-source",
            source_message_count=len(root),
            budget=budget,
            decision=retry_decision,
        )


def test_append_only_hard_limit_pauses_before_request_when_rebase_cannot_fit() -> None:
    root = [
        ModelMessage(role="system", content="static system"),
        ModelMessage(role="user", content="small task"),
    ]
    coordinator = PromptCacheCoordinator.bootstrap(
        root,
        _tools(),
        layout=PromptCacheLayout.APPEND_ONLY,
        prefix_message_count=2,
        epoch_id="initial",
    )
    source_request = coordinator.prepare_request(
        0,
        root,
        provider="fake",
        model="model-1",
        thinking={"enabled": False},
        request_id="hard-source",
    )
    coordinator.observe_response(source_request, _usage())
    suffix = [ModelMessage(role="user", content="pending input " + "x" * 40_000)]
    candidate = [*root, *suffix]
    budget = PrefixBudget(
        input_limit=30_000,
        ordinary_limit=1_000,
        soft_limit=500,
        memory_message_limit=256,
        summary_limit=256,
    )
    boundary = CacheEpochBoundary.CONTEXT_THRESHOLD
    decision = _compression_decision(
        coordinator,
        candidate,
        suffix,
        budget,
        step=1,
        boundary=boundary,
    )
    assert decision.action == "pause"
    assert decision.reason == "insufficient_rebase_headroom"
    with pytest.raises(PromptCacheCoordinatorError, match="decision does not match"):
        coordinator.prepare_compression(
            1,
            candidate,
            boundary=boundary,
            provider="fake",
            model="model-1",
            thinking={"enabled": False},
            source_messages=root,
            unsent_suffix_messages=suffix,
            source_request_id="hard-source",
            source_message_count=len(root),
            budget=budget,
            decision=decision,
        )


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


def _append_only_state(**overrides: object) -> AppendOnlyPromptState:
    values: dict[str, object] = {
        "root_prefix_message_count": 2,
        "last_submitted_message_count": 2,
        "last_submitted_message_fingerprints": ["a" * 64, "b" * 64],
        "last_submitted_request_id": "request-1",
    }
    values.update(overrides)
    return AppendOnlyPromptState(**values)  # type: ignore[arg-type]


def test_append_only_optimization_policy_versions_are_frozen_and_repeatable() -> None:
    baseline = AppendOnlyOptimizationPolicy.for_version("baseline_v1")
    balanced = AppendOnlyOptimizationPolicy.for_version("balanced_v1")

    assert baseline == AppendOnlyOptimizationPolicy.for_version("baseline_v1")
    assert baseline.projection_mode == "legacy"
    assert baseline.soft_limit_ratio == 0.8
    assert baseline.soft_compression_backoff_steps == 0
    assert baseline.summary_target_max_tokens is None
    assert baseline.fixed_projection_budget is False
    assert balanced == AppendOnlyOptimizationPolicy.for_version("balanced_v1")
    assert balanced.projection_mode == "structured_v1"
    assert balanced.soft_limit_ratio == 0.95
    assert balanced.soft_compression_backoff_steps == 3
    assert balanced.summary_target_max_tokens == 1_024
    assert balanced.fixed_projection_budget is True
    with pytest.raises(ValidationError, match="frozen"):
        balanced.soft_limit_ratio = 0.5


def test_balanced_compression_uses_95_percent_threshold_instead_of_baseline_80() -> None:
    budget = PrefixBudget(
        input_limit=1_200,
        ordinary_limit=1_000,
        soft_limit=800,
        memory_message_limit=128,
        summary_limit=128,
    )
    common = {
        "budget": budget,
        "candidate_input_tokens": 850,
        "mandatory_rebase_tokens": 200,
        "step": 10,
        "last_compression_attempt_step": None,
        "has_submitted_source": True,
    }

    baseline = decide_append_only_compression(
        policy=AppendOnlyOptimizationPolicy.for_version("baseline_v1"),
        **common,  # type: ignore[arg-type]
    )
    balanced = decide_append_only_compression(
        policy=AppendOnlyOptimizationPolicy.for_version("balanced_v1"),
        **common,  # type: ignore[arg-type]
    )

    assert baseline.action == "compress"
    assert baseline.reason == "soft_limit_exceeded"
    assert baseline.soft_limit == 800
    assert balanced.action == "continue"
    assert balanced.reason == "below_soft_limit"
    assert balanced.soft_limit == 950


def test_balanced_budget_reserves_its_final_instruction_and_separates_target() -> None:
    policy = AppendOnlyOptimizationPolicy.for_version("balanced_v1")
    budget = compute_prefix_budget(
        TaskBudget(max_context_tokens=16_000, max_output_tokens=2_000),
        None,
        _tools(),
        policy=policy,
    )

    assert budget.summary_target_tokens == min(budget.summary_limit, 1_024)
    assert "PATCHLOOP_EPOCH_COMPRESSION_BALANCED_V1" in budget.compression_instruction
    assert str(budget.summary_target_tokens) in budget.compression_instruction
    assert budget.input_limit - budget.ordinary_limit == (
        ContextEngine.estimate_message(
            ModelMessage(role="user", content=budget.compression_instruction)
        )
        + 64
    )

    baseline = compute_prefix_budget(
        TaskBudget(max_context_tokens=16_000, max_output_tokens=2_000),
        None,
        _tools(),
    )
    assert baseline.compression_instruction == COMPRESSION_INSTRUCTION
    assert baseline.summary_target_tokens is None


def test_balanced_soft_backoff_never_overrides_hard_or_oversized_limits() -> None:
    policy = AppendOnlyOptimizationPolicy.for_version("balanced_v1")
    budget = PrefixBudget(
        input_limit=1_200,
        ordinary_limit=1_000,
        soft_limit=950,
        memory_message_limit=128,
        summary_limit=128,
    )
    common = {
        "policy": policy,
        "budget": budget,
        "mandatory_rebase_tokens": 200,
        "step": 10,
        "last_compression_attempt_step": 8,
        "has_submitted_source": True,
    }

    backed_off = decide_append_only_compression(
        candidate_input_tokens=960,
        **common,  # type: ignore[arg-type]
    )
    hard = decide_append_only_compression(
        candidate_input_tokens=1_001,
        **common,  # type: ignore[arg-type]
    )
    oversized = decide_append_only_compression(
        candidate_input_tokens=900,
        oversized_delta=True,
        **common,  # type: ignore[arg-type]
    )

    assert backed_off.action == "continue"
    assert backed_off.reason == "soft_compression_backoff"
    assert hard.action == "compress"
    assert hard.reason == "hard_input_limit_exceeded"
    assert oversized.action == "compress"
    assert oversized.reason == "oversized_memory_delta"


@pytest.mark.parametrize(
    ("candidate", "has_source", "expected_action", "expected_reason"),
    [
        (960, True, "continue", "insufficient_rebase_headroom"),
        (1_001, True, "pause", "insufficient_rebase_headroom"),
        (960, False, "continue", "no_submitted_compression_source"),
        (1_001, False, "pause", "no_submitted_compression_source"),
    ],
)
def test_compression_preflight_handles_rebase_headroom_and_first_request(
    candidate: int,
    has_source: bool,
    expected_action: str,
    expected_reason: str,
) -> None:
    policy = AppendOnlyOptimizationPolicy.for_version("balanced_v1")
    budget = PrefixBudget(
        input_limit=1_200,
        ordinary_limit=1_000,
        soft_limit=950,
        memory_message_limit=128,
        summary_limit=128,
    )
    mandatory = 872 if has_source else 200
    decision = decide_append_only_compression(
        policy=policy,
        budget=budget,
        candidate_input_tokens=candidate,
        mandatory_rebase_tokens=mandatory,
        step=1,
        last_compression_attempt_step=None,
        has_submitted_source=has_source,
    )

    assert decision.action == expected_action
    assert decision.reason == expected_reason


def test_recovering_compression_replays_the_same_decision_after_restore() -> None:
    policy = AppendOnlyOptimizationPolicy.for_version("balanced_v1")
    budget = PrefixBudget(
        input_limit=1_200,
        ordinary_limit=1_000,
        soft_limit=950,
        memory_message_limit=128,
        summary_limit=128,
    )
    kwargs = {
        "policy": policy,
        "budget": budget,
        "candidate_input_tokens": 960,
        "mandatory_rebase_tokens": 900,
        "step": 7,
        "last_compression_attempt_step": 7,
        "has_submitted_source": True,
        "recovering_compression": True,
    }

    first = decide_append_only_compression(**kwargs)  # type: ignore[arg-type]
    restored = CompressionDecision.model_validate_json(first.model_dump_json())

    assert first == restored
    assert restored.action == "compress"
    assert restored.reason == "recovering_compression"


def test_prepare_compression_validates_decision_and_persists_attempt_step() -> None:
    coordinator, source, tools, publication = _append_only_compression_fixture()
    suffix = [ModelMessage(role="user", content="pending suffix")]
    candidate = [*source, *suffix]
    budget = PrefixBudget(
        input_limit=20_000,
        ordinary_limit=10_000,
        soft_limit=8_000,
        memory_message_limit=2_048,
        summary_limit=512,
    )
    mandatory = estimate_append_only_mandatory_rebase_tokens(
        root_messages=coordinator.frozen_prefix[:2],
        unsent_suffix_messages=suffix,
        publication_state=publication,
        tools=tools,
        memory_message_limit=budget.memory_message_limit,
    )
    policy = coordinator.optimization_policy
    assert policy is not None
    decision = decide_append_only_compression(
        policy=policy,
        budget=budget,
        candidate_input_tokens=(
            ContextEngine.estimate_messages(candidate) + ContextEngine.estimate_tools(tools)
        ),
        mandatory_rebase_tokens=mandatory,
        step=1,
        last_compression_attempt_step=None,
        has_submitted_source=True,
        oversized_delta=True,
    )

    with pytest.raises(ValueError, match="verified decision"):
        coordinator.prepare_compression(
            1,
            candidate,
            boundary=CacheEpochBoundary.EXPLICIT_COMPRESSION,
            provider="fake",
            model="model-1",
            thinking={"enabled": False},
            source_messages=source,
            unsent_suffix_messages=suffix,
            source_request_id="source-request-1",
            source_message_count=len(source),
            budget=budget,
            candidate_publication_state=publication,
        )

    with pytest.raises(PromptCacheCoordinatorError, match="decision does not match"):
        coordinator.prepare_compression(
            1,
            candidate,
            boundary=CacheEpochBoundary.EXPLICIT_COMPRESSION,
            provider="fake",
            model="model-1",
            thinking={"enabled": False},
            source_messages=source,
            unsent_suffix_messages=suffix,
            source_request_id="source-request-1",
            source_message_count=len(source),
            budget=budget,
            decision=decision.model_copy(
                update={"candidate_input_tokens": decision.candidate_input_tokens + 1}
            ),
            candidate_publication_state=publication,
        )

    prepared = coordinator.prepare_compression(
        1,
        candidate,
        boundary=CacheEpochBoundary.EXPLICIT_COMPRESSION,
        provider="fake",
        model="model-1",
        thinking={"enabled": False},
        source_messages=source,
        unsent_suffix_messages=suffix,
        source_request_id="source-request-1",
        source_message_count=len(source),
        budget=budget,
        decision=decision,
        candidate_publication_state=publication,
    )

    assert prepared.decision == decision
    assert coordinator.append_only_state is not None
    assert coordinator.append_only_state.last_compression_attempt_step == 1
    restored = PromptCacheCoordinator.from_snapshot(coordinator.snapshot())
    assert restored.append_only_state is not None
    assert restored.append_only_state.last_compression_attempt_step == 1


def test_old_append_only_state_defaults_to_baseline_policy() -> None:
    state = AppendOnlyPromptState.model_validate({"root_prefix_message_count": 2})

    assert state.optimization_version == "baseline_v1"
    assert state.last_compression_attempt_step is None


def test_balanced_policy_and_backoff_state_round_trip_without_changing_pending_request() -> None:
    state = _append_only_state(
        optimization_version="balanced_v1",
        last_compression_attempt_step=7,
        compression_request_id="compression-1",
        compression_max_output_tokens=321,
    )
    coordinator = _append_only_coordinator(state=state)

    assert coordinator.optimization_policy == AppendOnlyOptimizationPolicy.for_version(
        "balanced_v1"
    )
    restored = PromptCacheCoordinator.from_snapshot(coordinator.snapshot())
    assert restored.append_only_state == state
    assert restored.append_only_state.compression_request_id == "compression-1"
    assert restored.append_only_state.compression_max_output_tokens == 321
    assert restored.append_only_state.compression_instruction is None
    assert restored.append_only_state.compression_summary_target_tokens is None
    assert restored.append_only_state.last_compression_attempt_step == 7


def test_pending_compression_freezes_and_atomically_clears_instruction_target() -> None:
    instruction = "PATCHLOOP_EPOCH_COMPRESSION_BALANCED_V1\nexact frozen request"
    state = _append_only_state(
        optimization_version="balanced_v1",
        compression_instruction=instruction,
        compression_summary_target_tokens=512,
    )
    coordinator = _append_only_coordinator(state=state)

    coordinator.set_compression_request_id(
        "compression-1",
        max_output_tokens=1_024,
        instruction=instruction,
        summary_target_tokens=512,
    )
    restored = PromptCacheCoordinator.from_snapshot(coordinator.snapshot())
    assert restored.append_only_state is not None
    assert restored.append_only_state.compression_instruction == instruction
    assert restored.append_only_state.compression_summary_target_tokens == 512
    with pytest.raises(PromptCacheCoordinatorError, match="instruction changed while pending"):
        restored.set_compression_request_id(
            "compression-1", instruction="changed", summary_target_tokens=512
        )

    restored.set_compression_request_id(None)
    assert restored.append_only_state.compression_request_id is None
    assert restored.append_only_state.compression_max_output_tokens is None
    assert restored.append_only_state.compression_instruction is None
    assert restored.append_only_state.compression_summary_target_tokens is None


_UNSET: object = object()


def _append_only_coordinator(
    messages: list[ModelMessage] | None = None,
    state: AppendOnlyPromptState | object | None = _UNSET,
) -> PromptCacheCoordinator:
    transcript = _messages() if messages is None else messages
    epoch = CacheEpoch.bootstrap(
        transcript,
        prefix_message_count=2,
        epoch_id="initial",
    ).snapshot
    return PromptCacheCoordinator.from_legacy_state(
        layout=PromptCacheLayout.APPEND_ONLY,
        cache_epoch_id="initial",
        prefix_message_count=2,
        frozen_tools=_tools(),
        messages=transcript,
        cache_epoch_state=epoch,
        append_only_state=(
            _append_only_state() if state is _UNSET else state  # type: ignore[arg-type]
        ),
    )


def test_append_only_state_round_trips_through_coordinator() -> None:
    state = _append_only_state(
        last_submitted_tool_fingerprint="c" * 64,
        last_submitted_binding_fingerprint="d" * 64,
        epoch_generation=1,
        compression_source_request_id="request-1",
        compression_source_message_count=2,
        compression_request_id="compression-1",
        deferred_compression_fingerprint="e" * 64,
    )
    coordinator = _append_only_coordinator(state=state)

    assert coordinator.append_only_state == state
    assert coordinator.checkpoint_fields()["append_only_state"] == state

    restored = PromptCacheCoordinator.from_snapshot(coordinator.snapshot())
    assert restored.append_only_state == state
    assert restored.snapshot() == coordinator.snapshot()


def test_append_only_state_count_must_match_its_fingerprint_vector() -> None:
    with pytest.raises(ValidationError, match="fingerprint vector length"):
        _append_only_state(
            last_submitted_message_count=3,
            last_submitted_message_fingerprints=["a" * 64, "b" * 64],
        )


def test_append_only_state_fingerprints_must_be_sha256_digests() -> None:
    with pytest.raises(ValidationError, match="SHA-256"):
        _append_only_state(
            last_submitted_message_fingerprints=["a" * 64, "not-a-digest"],
        )
    with pytest.raises(ValidationError):
        _append_only_state(last_submitted_tool_fingerprint="short")


def test_append_only_state_compression_source_cannot_exceed_submission() -> None:
    with pytest.raises(ValidationError, match="last submitted message boundary"):
        _append_only_state(
            compression_source_request_id="request-1",
            compression_source_message_count=3,
        )


def test_append_only_coordinator_requires_state_and_epoch() -> None:
    with pytest.raises(ValueError, match="requires append-only state"):
        _append_only_coordinator(state=None)
    with pytest.raises(ValueError, match="requires an epoch"):
        PromptCacheCoordinator.from_legacy_state(
            layout=PromptCacheLayout.APPEND_ONLY,
            cache_epoch_id="initial",
            prefix_message_count=2,
            frozen_tools=_tools(),
            messages=_messages(),
            append_only_state=_append_only_state(),
        )


def test_legacy_and_stable_layouts_reject_append_only_state() -> None:
    state = _append_only_state()
    with pytest.raises(ValueError, match="only the append_only layout"):
        PromptCacheCoordinator.from_legacy_state(
            layout=PromptCacheLayout.STABLE,
            cache_epoch_id="initial",
            prefix_message_count=2,
            frozen_tools=_tools(),
            messages=_messages(),
            cache_epoch_state=CacheEpoch.bootstrap(
                _messages(), prefix_message_count=2, epoch_id="initial"
            ).snapshot,
            append_only_state=state,
        )
    with pytest.raises(ValueError, match="only the append_only layout"):
        PromptCacheCoordinator.from_legacy_state(
            layout=PromptCacheLayout.LEGACY,
            cache_epoch_id="initial",
            prefix_message_count=2,
            frozen_tools=_tools(),
            messages=_messages(),
            append_only_state=state,
        )


def test_append_only_snapshot_rejects_missing_and_foreign_state() -> None:
    snapshot = _append_only_coordinator().snapshot()
    without_state = snapshot.model_dump(mode="json")
    without_state["append_only_state"] = None
    with pytest.raises(ValidationError, match="requires append-only state"):
        PromptCacheCoordinatorSnapshot.model_validate(without_state)

    stable = PromptCacheCoordinator.bootstrap(
        _messages(),
        _tools(),
        layout=PromptCacheLayout.STABLE,
        prefix_message_count=2,
    ).snapshot()
    foreign = stable.model_dump(mode="json")
    foreign["append_only_state"] = snapshot.model_dump(mode="json")["append_only_state"]
    with pytest.raises(ValidationError, match="only the append_only layout"):
        PromptCacheCoordinatorSnapshot.model_validate(foreign)


def test_append_only_restore_rejects_boundaries_beyond_the_transcript() -> None:
    beyond_submission = _append_only_state(
        last_submitted_message_count=4,
        last_submitted_message_fingerprints=["a" * 64, "b" * 64, "c" * 64, "d" * 64],
    )
    with pytest.raises(ValueError, match="shorter than its last submitted request"):
        _append_only_coordinator(messages=_messages(), state=beyond_submission)

    oversized_root = _append_only_state(root_prefix_message_count=4)
    with pytest.raises(ValueError, match="shorter than its declared root prefix"):
        _append_only_coordinator(messages=_messages(), state=oversized_root)
