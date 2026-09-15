from __future__ import annotations

import json

import pytest

from patchloop.context.engine import ContextEngine
from patchloop.memory.working import WORKING_MEMORY_PREFIX
from patchloop.prompt_cache import (
    MEMORY_DELTA_PREFIX,
    MEMORY_DELTA_V2_PREFIX,
    MEMORY_SNAPSHOT_PREFIX,
    MEMORY_SNAPSHOT_V2_PREFIX,
    MemoryDeltaPublisher,
    MemoryDeltaTooLarge,
)


def _projection(payload: dict[str, object]) -> str:
    return "PATCHLOOP_PROVIDER_MEMORY_V1\nnotice\n" + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def test_publisher_sends_one_snapshot_and_no_message_for_unchanged_reads() -> None:
    payload = {"facts": [{"scope": "repository", "text": "MODE is stable", "type": "code_symbol"}]}
    publisher, message = MemoryDeltaPublisher().publish("epoch-1", _projection(payload))

    assert message is not None
    assert message.content.startswith(MEMORY_SNAPSHOT_PREFIX)
    unchanged, no_message = publisher.publish("epoch-1", _projection(payload))
    assert no_message is None
    assert unchanged.snapshot == publisher.snapshot
    assert len(unchanged.messages) == 1


def test_delta_is_append_only_deduplicated_and_replayable() -> None:
    first = {"facts": [{"scope": "repository", "text": "MODE is stable", "type": "code_symbol"}]}
    second = {
        "facts": [
            {"scope": "repository", "text": "MODE is stable", "type": "code_symbol"},
            {"scope": "task", "text": "run tests", "type": "verification"},
        ]
    }
    publisher, _ = MemoryDeltaPublisher().publish("epoch-1", _projection(first))
    publisher, delta = publisher.publish("epoch-1", _projection(second))

    assert delta is not None
    assert delta.content.startswith(MEMORY_DELTA_PREFIX)
    assert len(publisher.messages) == 2
    assert MemoryDeltaPublisher.replay(publisher.snapshot) == publisher.snapshot.current_payload

    restored = MemoryDeltaPublisher(publisher.snapshot)
    restored, no_duplicate = restored.publish("epoch-1", _projection(second))
    assert no_duplicate is None
    assert len(restored.messages) == 2


def test_epoch_change_starts_a_new_snapshot_stream() -> None:
    publisher, _ = MemoryDeltaPublisher().publish("epoch-1", _projection({"facts": []}))
    next_publisher, message = publisher.publish("epoch-2", _projection({"facts": []}))

    assert message is not None
    assert message.content.startswith(MEMORY_SNAPSHOT_PREFIX)
    assert next_publisher.snapshot.epoch_id == "epoch-2"
    assert len(next_publisher.messages) == 1


def test_large_delta_is_rejected_without_replacing_the_previous_state() -> None:
    publisher, _ = MemoryDeltaPublisher(max_delta_tokens=64).publish(
        "epoch-1",
        _projection({"facts": [{"text": "old", "type": "fact", "scope": "task"}]}),
    )
    with pytest.raises(MemoryDeltaTooLarge):
        publisher.publish(
            "epoch-1",
            _projection({"facts": [{"text": "new " * 100, "type": "fact", "scope": "task"}]}),
        )


def _v2_envelope(message, prefix: str) -> dict[str, object]:
    return json.loads(message.content[len(prefix) :])


def _item(value: str) -> dict[str, object]:
    return {"scope": "task", "text": value, "type": "fact"}


def test_v2_replays_repeated_state_transitions_without_global_body_deduplication() -> None:
    publisher = MemoryDeltaPublisher()
    state_a = {"facts": [_item("A")]}
    state_b = {"facts": [_item("B")]}
    update = publisher.preview(
        "epoch-1", _projection(state_a), invalidated_values=[], max_message_tokens=2_048
    )
    assert update.messages[0].role == "user"
    assert update.messages[0].content.startswith(MEMORY_SNAPSHOT_V2_PREFIX)
    assert _v2_envelope(update.messages[0], MEMORY_SNAPSHOT_V2_PREFIX)["sequence"] == 0
    publisher = MemoryDeltaPublisher(update.next_state)

    sequences = []
    for payload in (state_b, state_a, state_b):
        update = publisher.preview(
            "epoch-1", _projection(payload), invalidated_values=[], max_message_tokens=2_048
        )
        assert len(update.messages) == 1
        assert update.messages[0].content.startswith(MEMORY_DELTA_V2_PREFIX)
        sequences.append(_v2_envelope(update.messages[0], MEMORY_DELTA_V2_PREFIX)["sequence"])
        publisher = MemoryDeltaPublisher(update.next_state)

    assert sequences == [1, 2, 3]
    assert update.next_state.delta_count == 3
    assert MemoryDeltaPublisher.replay(update.next_state) == update.next_state.current_payload
    assert MemoryDeltaPublisher.replay_state(update.next_state) == (
        update.next_state.current_payload,
        [],
    )


def test_v2_unchanged_projection_and_ranking_order_do_not_publish() -> None:
    first = {"facts": [_item("B"), _item("A")]}
    publisher = MemoryDeltaPublisher()
    initial = publisher.preview(
        "epoch-1", _projection(first), invalidated_values=[], max_message_tokens=2_048
    )
    restored = MemoryDeltaPublisher(initial.next_state)

    reordered = {"facts": [_item("A"), _item("B")]}
    unchanged = restored.preview(
        "epoch-1", _projection(reordered), invalidated_values=[], max_message_tokens=2_048
    )

    assert unchanged.messages == []
    assert unchanged.next_state == initial.next_state
    assert unchanged.next_state.delta_count == 0


def test_v2_distinguishes_retrieval_failure_from_a_successful_empty_projection() -> None:
    publisher = MemoryDeltaPublisher()
    initial = publisher.preview(
        "epoch-1",
        _projection({"facts": [_item("still visible")]}),
        invalidated_values=[],
        max_message_tokens=2_048,
    )
    restored = MemoryDeltaPublisher(initial.next_state)

    failed_retrieval = restored.preview(
        "epoch-1", None, invalidated_values=[], max_message_tokens=2_048
    )
    assert failed_retrieval.messages == []
    assert failed_retrieval.next_state.current_payload == initial.next_state.current_payload

    successful_empty = restored.preview(
        "epoch-1", _projection({}), invalidated_values=[], max_message_tokens=2_048
    )
    assert len(successful_empty.messages) == 1
    assert successful_empty.next_state.current_payload == {}
    assert MemoryDeltaPublisher.replay(successful_empty.next_state) == {}


def test_v2_invalidations_are_ordered_data_and_can_be_cleared() -> None:
    publisher = MemoryDeltaPublisher()
    initial = publisher.preview(
        "epoch-1",
        _projection({"facts": [_item("old value")]}),
        invalidated_values=[],
        max_message_tokens=2_048,
    )
    restored = MemoryDeltaPublisher(initial.next_state)

    invalidated = restored.preview(
        "epoch-1",
        None,
        invalidated_values=["old value", "older value", "old value"],
        max_message_tokens=2_048,
    )
    assert invalidated.next_state.invalidated_values == ["old value", "older value"]
    assert MemoryDeltaPublisher.replay_state(invalidated.next_state) == (
        initial.next_state.current_payload,
        ["old value", "older value"],
    )

    revalidated = MemoryDeltaPublisher(invalidated.next_state).preview(
        "epoch-1",
        None,
        invalidated_values=[],
        max_message_tokens=2_048,
    )
    assert MemoryDeltaPublisher.replay_state(revalidated.next_state) == (
        initial.next_state.current_payload,
        [],
    )


def test_v2_preview_failure_is_atomic_and_retry_keeps_the_next_sequence() -> None:
    publisher = MemoryDeltaPublisher()
    initial = publisher.preview(
        "epoch-1", _projection({}), invalidated_values=[], max_message_tokens=2_048
    )
    committed = initial.next_state
    publisher = MemoryDeltaPublisher(committed)

    with pytest.raises(MemoryDeltaTooLarge, match="one memory item"):
        publisher.preview(
            "epoch-1",
            _projection({"facts": [_item("a"), _item("too large " * 100)]}),
            invalidated_values=[],
            max_message_tokens=256,
        )
    assert publisher.snapshot == committed

    retry = publisher.preview(
        "epoch-1",
        _projection({"facts": [_item("accepted")]}),
        invalidated_values=[],
        max_message_tokens=512,
    )
    assert _v2_envelope(retry.messages[0], MEMORY_DELTA_V2_PREFIX)["sequence"] == 1
    assert publisher.snapshot == committed


def test_v2_delta_chunks_are_deterministic_and_replay_as_one_update() -> None:
    publisher = MemoryDeltaPublisher()
    initial = publisher.preview(
        "epoch-1",
        _projection({}),
        invalidated_values=[],
        max_message_tokens=256,
    )
    publisher = MemoryDeltaPublisher(initial.next_state)
    items = [_item(f"value-{index}: " + ("x" * 70)) for index in range(5)]
    projection = _projection({"facts": items})

    first = publisher.preview("epoch-1", projection, invalidated_values=[], max_message_tokens=256)
    second = publisher.preview("epoch-1", projection, invalidated_values=[], max_message_tokens=256)

    assert len(first.messages) > 1
    assert first.messages == second.messages
    assert first.next_state == second.next_state
    assert all(ContextEngine.estimate_message(message) <= 256 for message in first.messages)
    assert MemoryDeltaPublisher.replay(first.next_state) == first.next_state.current_payload
    assert first.next_state.delta_count == len(first.next_state.messages) - 1


def test_v2_initial_snapshot_obeys_message_budget() -> None:
    with pytest.raises(MemoryDeltaTooLarge, match="memory snapshot"):
        MemoryDeltaPublisher().preview(
            "epoch-1",
            _projection({"facts": [_item("x" * 300)]}),
            invalidated_values=[],
            max_message_tokens=64,
        )


def test_v2_json_escaping_is_included_in_snapshot_and_delta_budgets() -> None:
    projection = _projection({"facts": [_item('a "quoted" path\\part\n' * 10)]})
    initial = MemoryDeltaPublisher().preview(
        "epoch-1", projection, invalidated_values=[], max_message_tokens=2_048
    )
    limit = ContextEngine.estimate_message(initial.messages[0]) - 1
    with pytest.raises(MemoryDeltaTooLarge, match="memory snapshot"):
        MemoryDeltaPublisher().preview(
            "epoch-1", projection, invalidated_values=[], max_message_tokens=limit
        )
    publisher = MemoryDeltaPublisher(initial.next_state)
    with pytest.raises(MemoryDeltaTooLarge, match="memory snapshot"):
        publisher.rebase_snapshot("epoch-2", max_message_tokens=limit)

    empty = MemoryDeltaPublisher().preview(
        "epoch-1", _projection({}), invalidated_values=[], max_message_tokens=2_048
    )
    publisher = MemoryDeltaPublisher(empty.next_state)
    update = publisher.preview(
        "epoch-1", projection, invalidated_values=[], max_message_tokens=2_048
    )
    limit = ContextEngine.estimate_message(update.messages[0]) - 1
    with pytest.raises(MemoryDeltaTooLarge, match="one memory item"):
        publisher.preview("epoch-1", projection, invalidated_values=[], max_message_tokens=limit)
    assert publisher.snapshot == empty.next_state


def _working_projection(revision: int, read_files: list[str]) -> str:
    return _projection(
        {
            "working_state": [
                {
                    "text": WORKING_MEMORY_PREFIX
                    + json.dumps(
                        {
                            "revision": revision,
                            "goal": "user_message",
                            "constraints": ["preserve exact whitespace validation"],
                            "read_files": read_files,
                        }
                    )
                }
            ]
        }
    )


def test_v2_working_memory_only_publishes_changed_facts_and_ignores_revision() -> None:
    first = MemoryDeltaPublisher().preview(
        "epoch-1",
        _working_projection(1, ["contract.md"]),
        invalidated_values=[],
        max_message_tokens=2_048,
    )
    publisher = MemoryDeltaPublisher(first.next_state)
    unchanged = publisher.preview(
        "epoch-1",
        _working_projection(2, ["contract.md"]),
        invalidated_values=[],
        max_message_tokens=2_048,
    )
    assert unchanged.messages == []
    changed = publisher.preview(
        "epoch-1",
        _working_projection(3, ["contract.md", "service.py"]),
        invalidated_values=[],
        max_message_tokens=512,
    )
    assert len(changed.messages) == 1
    envelope = _v2_envelope(changed.messages[0], MEMORY_DELTA_V2_PREFIX)
    assert envelope["payload"]["removed"] == {}
    assert envelope["payload"]["added"]["working_state"] == [
        {"type": "working_memory", "field": "read_files", "index": 1, "value": "service.py"}
    ]
    assert "whitespace" not in changed.messages[0].content
    replayed = MemoryDeltaPublisher.replay(changed.next_state)
    assert {entry["value"] for entry in replayed["working_state"]} == {
        "user_message",
        "preserve exact whitespace validation",
        "contract.md",
        "service.py",
    }
    assert (
        changed.next_state.messages[: len(first.next_state.messages)] == first.next_state.messages
    )


def test_v2_existing_working_blob_upgrades_by_appending_without_rewriting_history() -> None:
    projection = _working_projection(1, ["contract.md"])
    # V1-to-V2 upgrade retains the old opaque working item in its first snapshot.
    legacy, _ = MemoryDeltaPublisher().publish("epoch-1", projection)
    upgraded = legacy.preview("epoch-1", None, invalidated_values=[], max_message_tokens=2_048)
    old_messages = upgraded.next_state.messages.copy()
    changed = MemoryDeltaPublisher(upgraded.next_state).preview(
        "epoch-1", projection, invalidated_values=[], max_message_tokens=2_048
    )
    assert changed.next_state.messages[: len(old_messages)] == old_messages
    assert all(
        "field" in item for item in MemoryDeltaPublisher.replay(changed.next_state)["working_state"]
    )


def test_v2_preview_can_upgrade_v1_state_without_mutating_it() -> None:
    publisher, _ = MemoryDeltaPublisher().publish(
        "epoch-1",
        _projection({"facts": [_item("old")]}),
    )
    original_v1_state = publisher.snapshot

    update = publisher.preview(
        "epoch-1",
        _projection({"facts": [_item("new")]}),
        invalidated_values=["old"],
        max_message_tokens=2_048,
    )

    assert original_v1_state is not None
    assert original_v1_state.schema_version == "1.0"
    assert publisher.snapshot is original_v1_state
    assert update.next_state.schema_version == "2.0"
    assert update.messages[0].content.startswith(MEMORY_SNAPSHOT_V2_PREFIX)
    assert update.messages[1].content.startswith(MEMORY_DELTA_V2_PREFIX)
    assert MemoryDeltaPublisher.replay_state(update.next_state) == (
        update.next_state.current_payload,
        ["old"],
    )
