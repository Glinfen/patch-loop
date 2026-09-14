"""PGW-07: durable provider request, attempt, and continuation semantics."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from patchloop.context.engine import ContextEngine
from patchloop.domain import AgentStep, Task
from patchloop.execution.models import Effect, Execution
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import (
    LeaseGuard,
    LeaseLost,
    ProviderAttemptOutcome,
    ProviderAttemptRecord,
    ProviderAttemptStatus,
    ProviderRequestConflict,
    ProviderRequestRecord,
    ProviderRequestStatus,
    ProviderUsageStatus,
)
from patchloop.prompt_cache.diagnostics import fingerprint_request
from patchloop.providers.base import ModelMessage, ModelResponse, ModelUsage, ProviderRequestPurpose
from patchloop.providers.continuation import (
    ContinuationCodec,
    ContinuationIntegrityError,
    ContinuationUnavailable,
    estimate_continuation_tokens,
)
from patchloop.providers.contracts import (
    ChatDialect,
    ProviderBinding,
    ProviderCapabilities,
    ProviderContinuation,
    ProviderGeneration,
    ProviderProtocol,
    ProviderTransportConfig,
    ReasoningTransport,
    ValidatedResponseItem,
)
from patchloop.session.models import Session, SessionCheckpoint


def _binding(*, deepseek: bool = False) -> ProviderBinding:
    return ProviderBinding(
        profile_id="pgw07-test",
        protocol=ProviderProtocol.CHAT_COMPLETIONS,
        dialect=ChatDialect.DEEPSEEK if deepseek else ChatDialect.STANDARD,
        model="pgw07-model",
        base_url="https://provider.example/v1",
        auth="none",
        capabilities=ProviderCapabilities(
            tools=True,
            streaming=False,
            reasoning_transport=(
                ReasoningTransport.DEEPSEEK_TEXT if deepseek else ReasoningTransport.NONE
            ),
            context_window_tokens=4096,
            max_output_tokens=1024,
            usage_supported=True,
        ),
        generation=ProviderGeneration(
            max_output_tokens=512,
            reasoning_enabled=deepseek,
            reasoning_effort="medium" if deepseek else None,
        ),
        transport=ProviderTransportConfig(streaming=False),
    )


def _owned_store(path: Path) -> tuple[SQLiteStore, Task, LeaseGuard]:
    store = SQLiteStore(path)
    session = store.create_session(Session(id="session-pgw07", workspace_ref="workspace"))
    task = store.start_task(
        session.id,
        Task(id="task-pgw07", goal="persist provider responses", repository="workspace"),
        expected_version=session.version,
    )
    execution = store.claim_execution(
        Execution(
            id="execution-pgw07",
            session_id=session.id,
            task_id=task.id,
            owner_id="worker-pgw07",
            lease_token="lease-pgw07",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
        expected_version=task.version,
    )
    guard = LeaseGuard(
        execution.id,
        task.id,
        execution.lease_token,
        execution.generation,
        execution.owner_id,
    )
    return store, store.get_task(task.id), guard


def _request(
    binding: ProviderBinding,
    *,
    request_id: str = "provider-request-1",
    step_index: int = 0,
    input_revision: int = 0,
) -> ProviderRequestRecord:
    return ProviderRequestRecord(
        request_id=request_id,
        task_id="task-pgw07",
        purpose=ProviderRequestPurpose.AGENT_STEP,
        step_index=step_index,
        epoch_generation=0,
        input_revision=input_revision,
        binding_fingerprint=binding.fingerprint,
        input_digest=ContinuationCodec.input_digest(
            {"messages": [{"role": "user", "content": f"revision {input_revision}"}]}
        ),
    )


def _attempt(
    request: ProviderRequestRecord,
    guard: LeaseGuard,
    *,
    attempt_id: str = "provider-attempt-1",
    ordinal: int = 1,
) -> ProviderAttemptRecord:
    return ProviderAttemptRecord(
        attempt_id=attempt_id,
        request_id=request.request_id,
        execution_id=guard.execution_id,
        ordinal=ordinal,
        budget_reservation_usd=0.025,
    )


def _step_and_effect(
    response: ModelResponse, *, step_index: int = 0
) -> tuple[AgentStep, list[Effect]]:
    step_id = f"step-{step_index}"
    effects = [
        Effect(
            id=f"effect-{step_index}",
            task_id="task-pgw07",
            step_id=step_id,
            batch_position=0,
            provider_call_id=f"call-{step_index}",
            tool_name="write_file",
            action_kind="write",
            arguments_summary={"path": "result.txt"},
        )
    ]
    return (
        AgentStep(
            id=step_id,
            task_id="task-pgw07",
            index=step_index,
            model_response=response.model_dump(mode="json"),
            effect_ids=[effect.id for effect in effects],
        ),
        effects,
    )


def test_provider_response_and_effect_batch_commit_atomically_and_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, task, guard = _owned_store(tmp_path / "provider.sqlite")
    binding = _binding()
    request = _request(binding)
    store.begin_provider_request(request, lease_guard=guard)
    attempt = _attempt(request, guard)
    store.begin_provider_attempt(attempt, lease_guard=guard)
    response = ModelResponse(
        content="complete response",
        tool_calls=[],
        usage=ModelUsage(input_tokens=12, output_tokens=7),
    )

    saved = store.commit_provider_response(request.request_id, response, lease_guard=guard)
    assert saved.status is ProviderRequestStatus.RESPONSE_READY
    assert (
        store.list_provider_attempts(request.request_id)[0].status
        is ProviderAttemptStatus.SUCCEEDED
    )
    reopened = SQLiteStore(tmp_path / "provider.sqlite")
    assert reopened.get_provider_request(request.request_id).response == saved.response
    assert store.begin_provider_request(request, lease_guard=guard) == saved
    assert len(store.list_provider_attempts(request.request_id)) == 1
    session_checkpoint = SessionCheckpoint(
        session_id=task.session_id or "session-pgw07",
        task_id=task.id,
        pending_provider_request_id=request.request_id,
        accounted_provider_attempt_ids=[attempt.attempt_id],
    )
    store.commit_checkpoint(
        session_checkpoint,
        expected_version=task.version,
        lease_guard=guard,
    )
    restored_checkpoint = store.get_session_checkpoint(task.id)
    assert restored_checkpoint.pending_provider_request_id == request.request_id
    assert restored_checkpoint.accounted_provider_attempt_ids == [attempt.attempt_id]
    step, effects = _step_and_effect(response)

    original_journal = store._journal

    def fail_effect_journal(
        connection: sqlite3.Connection,
        session: Session,
        *,
        event_id: str,
        event_type: str,
        task_id: str | None = None,
        trace_id: str | None = None,
        data: dict[str, object] | None = None,
    ) -> tuple[object, Session]:
        if event_type == "effects.prepared":
            raise RuntimeError("injected effect journal failure")
        return original_journal(
            connection,
            session,
            event_id=event_id,
            event_type=event_type,
            task_id=task_id,
            trace_id=trace_id,
            data=data,
        )

    monkeypatch.setattr(store, "_journal", fail_effect_journal)
    with pytest.raises(RuntimeError, match="injected"):
        store.prepare_effect_batch(
            step,
            effects,
            expected_version=task.version,
            lease_guard=guard,
            provider_request_id=request.request_id,
        )
    assert (
        store.get_provider_request(request.request_id).status
        is ProviderRequestStatus.RESPONSE_READY
    )
    assert store.list_steps(task.id) == []
    assert store.list_effects(task.id) == []
    monkeypatch.undo()

    prepared = store.prepare_effect_batch(
        step,
        effects,
        expected_version=task.version,
        lease_guard=guard,
        provider_request_id=request.request_id,
    )
    assert [effect.id for effect in prepared] == ["effect-0"]
    assert store.get_provider_request(request.request_id).status is ProviderRequestStatus.COMPLETED
    assert (
        store.prepare_effect_batch(
            step,
            effects,
            expected_version=task.version,
            lease_guard=guard,
            provider_request_id=request.request_id,
        )
        == prepared
    )

    with pytest.raises(ProviderRequestConflict, match="differs"):
        store.prepare_effect_batch(
            step.model_copy(
                update={"model_response": {**step.model_response, "content": "different"}}
            ),
            effects,
            expected_version=task.version,
            lease_guard=guard,
            provider_request_id=request.request_id,
        )

    # An ordinary no-tool response follows the same settlement path.
    second = _request(binding, request_id="provider-request-2", step_index=1, input_revision=1)
    store.begin_provider_request(second, lease_guard=guard)
    second_attempt = _attempt(second, guard, attempt_id="provider-attempt-2")
    store.begin_provider_attempt(second_attempt, lease_guard=guard)
    plain_response = ModelResponse(
        content="done", usage=ModelUsage(input_tokens=3, output_tokens=2)
    )
    store.commit_provider_response(second.request_id, plain_response, lease_guard=guard)
    store.prepare_effect_batch(
        AgentStep(
            id="step-1",
            task_id=task.id,
            index=1,
            model_response=plain_response.model_dump(mode="json"),
            effect_ids=[],
        ),
        [],
        expected_version=task.version,
        lease_guard=guard,
        provider_request_id=second.request_id,
    )
    assert store.get_provider_request(second.request_id).status is ProviderRequestStatus.COMPLETED

    compression = _request(
        binding,
        request_id="compression-request-1",
        step_index=2,
        input_revision=1,
    ).model_copy(update={"purpose": ProviderRequestPurpose.EPOCH_COMPRESSION})
    store.begin_provider_request(compression, lease_guard=guard)
    compression_attempt = _attempt(
        compression,
        guard,
        attempt_id="compression-attempt-1",
    )
    store.begin_provider_attempt(compression_attempt, lease_guard=guard)
    store.commit_provider_response(
        compression.request_id,
        ModelResponse(content="compressed context"),
        lease_guard=guard,
    )
    assert (
        store.get_provider_request(compression.request_id).status is ProviderRequestStatus.COMPLETED
    )


def test_provider_attempt_recovery_fences_old_owner_and_separates_input_revisions(
    tmp_path: Path,
) -> None:
    store, task, old_guard = _owned_store(tmp_path / "provider-recovery.sqlite")
    binding = _binding()
    request = _request(binding)
    store.begin_provider_request(request, lease_guard=old_guard)
    store.begin_provider_attempt(_attempt(request, old_guard), lease_guard=old_guard)

    store.release_execution(old_guard, now=datetime.now(UTC))
    next_execution = store.claim_execution(
        Execution(
            id="execution-pgw07-resume",
            session_id=task.session_id or "session-pgw07",
            task_id=task.id,
            owner_id="worker-resume",
            lease_token="lease-pgw07-resume",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
        expected_version=store.get_task(task.id).version,
    )
    new_guard = LeaseGuard(
        next_execution.id,
        task.id,
        next_execution.lease_token,
        next_execution.generation,
        next_execution.owner_id,
    )

    with pytest.raises(LeaseLost):
        store.commit_provider_response(
            request.request_id,
            ModelResponse(content="late response"),
            lease_guard=old_guard,
        )
    resumed_attempt = _attempt(
        request,
        new_guard,
        attempt_id="provider-attempt-2",
        ordinal=2,
    )
    store.begin_provider_attempt(resumed_attempt, lease_guard=new_guard)
    attempts = store.list_provider_attempts(request.request_id)
    assert [attempt.status for attempt in attempts] == [
        ProviderAttemptStatus.INTERRUPTED,
        ProviderAttemptStatus.RUNNING,
    ]
    assert attempts[0].usage_status is ProviderUsageStatus.UNKNOWN
    with pytest.raises(LeaseLost):
        store.finish_provider_attempt(
            attempts[0].attempt_id,
            ProviderAttemptOutcome(status=ProviderAttemptStatus.FAILED),
            lease_guard=old_guard,
        )

    changed = _request(binding, request_id="provider-request-revision-1", input_revision=1)
    store.begin_provider_request(changed, lease_guard=new_guard)
    assert changed.request_id != request.request_id
    assert changed.input_digest != request.input_digest
    changed_attempt = _attempt(
        changed,
        new_guard,
        attempt_id="provider-attempt-revision-1",
    )
    store.begin_provider_attempt(changed_attempt, lease_guard=new_guard)
    cancelled = store.finish_provider_attempt(
        changed_attempt.attempt_id,
        ProviderAttemptOutcome(
            status=ProviderAttemptStatus.CANCELLED,
            error_kind="cancelled",
            safe_message="Bearer abcdefghijklmnop",
            request_sent=True,
        ),
        lease_guard=new_guard,
    )
    assert cancelled.status is ProviderAttemptStatus.CANCELLED
    assert cancelled.safe_message == "Bearer [REDACTED]"
    assert cancelled.usage_status is ProviderUsageStatus.UNKNOWN
    store.invalidate_provider_request(request.request_id, lease_guard=new_guard)
    assert (
        store.get_provider_request(request.request_id).status is ProviderRequestStatus.INVALIDATED
    )


def test_continuation_codec_preserves_ciphertext_and_blocks_redacted_replay(
    tmp_path: Path,
) -> None:
    codec = ContinuationCodec()
    ciphertext = "sk-abcdefgh12345678"
    responses_item = ValidatedResponseItem(
        type="reasoning",
        item={
            "id": "rs_opaque",
            "type": "reasoning",
            "status": "completed",
            "summary": [],
            "encrypted_content": ciphertext,
        },
    )
    response = ModelResponse(
        content="safe answer",
        continuation=ProviderContinuation(responses_items=(responses_item,)),
    )
    stored = codec.to_storage(response)
    assert stored.continuation is not None
    assert stored.continuation.responses_items[0].item["encrypted_content"] == ciphertext
    assert stored.continuation.replayable
    responses_binding = ProviderBinding(
        profile_id="responses-profile",
        protocol=ProviderProtocol.RESPONSES,
        model="responses-model",
        base_url="https://provider.example/v1",
        auth="none",
        capabilities=ProviderCapabilities(
            streaming=False,
            reasoning_transport=ReasoningTransport.RESPONSES_ITEMS,
            context_window_tokens=4096,
            max_output_tokens=1024,
        ),
        generation=ProviderGeneration(max_output_tokens=512, reasoning_enabled=True),
        transport=ProviderTransportConfig(streaming=False),
    )
    codec.validate_for_replay(
        stored,
        binding=responses_binding,
        binding_fingerprint=responses_binding.fingerprint,
        response_sha256=codec.response_digest(stored),
    )

    public = json.dumps(codec.to_public(response), ensure_ascii=False)
    assert ciphertext not in public
    assert "sha256" in public and "length" in public
    message = ModelMessage(
        role="assistant", content=response.content, continuation=stored.continuation
    )
    normalized = ContextEngine(
        max_tokens=512,
        max_tool_output_chars=128,
        recent_steps=1,
    ).normalize_new_messages([message])[0]
    assert normalized.continuation == stored.continuation
    assert estimate_continuation_tokens(normalized) >= len(ciphertext.encode("utf-8"))

    cache_wire = fingerprint_request([message], [])[1]
    assert ciphertext.encode() not in cache_wire
    assert b"sha256" in cache_wire

    deepseek = _binding(deepseek=True)
    private_response = ModelResponse(
        content="visible answer",
        continuation=ProviderContinuation(deepseek_reasoning_content="Bearer abcdefghijklmnop"),
    )
    safe_private = codec.to_storage(private_response)
    assert safe_private.continuation is not None
    assert safe_private.continuation.deepseek_reasoning_content != "Bearer abcdefghijklmnop"
    assert not safe_private.continuation.replayable
    with pytest.raises(ContinuationUnavailable, match="redacted"):
        codec.validate_for_replay(
            safe_private,
            binding=deepseek,
            binding_fingerprint=deepseek.fingerprint,
            response_sha256=codec.response_digest(safe_private),
        )
    with pytest.raises(ContinuationIntegrityError):
        codec.validate_for_replay(
            stored,
            binding=responses_binding,
            binding_fingerprint=responses_binding.fingerprint,
            response_sha256="0" * 64,
        )


def test_response_integrity_tampering_blocks_read_and_journal_has_no_response_body(
    tmp_path: Path,
) -> None:
    store, task, guard = _owned_store(tmp_path / "provider-tamper.sqlite")
    binding = _binding(deepseek=True)
    request = _request(binding)
    store.begin_provider_request(request, lease_guard=guard)
    store.begin_provider_attempt(_attempt(request, guard), lease_guard=guard)
    response = ModelResponse(
        content="answer",
        continuation=ProviderContinuation(deepseek_reasoning_content="private thought"),
    )
    committed = store.commit_provider_response(request.request_id, response, lease_guard=guard)
    assert committed.response is not None
    assert committed.response.continuation is not None
    events = store.list_events(task.session_id or "session-pgw07")
    saved = next(event for event in events if event.type == "provider.response.saved")
    assert "private thought" not in json.dumps(saved.data)
    assert "response_sha256" in saved.data

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE provider_requests SET response_json = '{}' WHERE id = ?",
            (request.request_id,),
        )
    with pytest.raises(ProviderRequestConflict, match="inconsistent"):
        store.get_provider_request(request.request_id)
