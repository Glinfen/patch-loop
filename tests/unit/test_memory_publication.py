from __future__ import annotations

import json

import pytest

from patchloop.prompt_cache import (
    MEMORY_DELTA_PREFIX,
    MEMORY_SNAPSHOT_PREFIX,
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
