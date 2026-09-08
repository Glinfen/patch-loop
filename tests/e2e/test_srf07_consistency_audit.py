"""SRF-07 independent audit cross-check for recovery and event integrity."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from patchloop.domain import Task, TaskStatus, ToolCall, ToolResult
from patchloop.events import SessionEvent, SessionEventExporter
from patchloop.execution.models import EffectStatus
from patchloop.execution.ownership import ExecutionOwnershipManager
from patchloop.persistence import SQLiteStore
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.runtime import AgentRuntime
from patchloop.tools import PermissionLevel, ReplaceTextTool, ToolContext, ToolGateway, ToolPolicy


class _AuditAfterMutationGateway(ToolGateway):
    def __init__(self, repository: Path, audit_path: Path) -> None:
        super().__init__(
            ToolContext(repository),
            [ReplaceTextTool()],
            policy=ToolPolicy(
                frozenset({PermissionLevel.WRITE}),
                approval_threshold=None,
                require_plan_for_mutations=False,
            ),
        )
        self.audit_path = audit_path

    def execute_claimed(
        self,
        task_id: str,
        call: ToolCall,
        *,
        approval_consumed: bool,
    ) -> ToolResult:
        super().execute_claimed(
            task_id,
            call,
            approval_consumed=approval_consumed,
        )
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": "external_action", "call_id": call.id}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        raise KeyboardInterrupt("crash after independently audited mutation")


def _gateway(repository: Path) -> ToolGateway:
    return ToolGateway(
        ToolContext(repository),
        [ReplaceTextTool()],
        policy=ToolPolicy(
            frozenset({PermissionLevel.WRITE}),
            approval_threshold=None,
            require_plan_for_mutations=False,
        ),
    )


def test_independent_audit_matches_database_trace_and_final_file(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    target = repository / "state.txt"
    target.write_text("Before\n", encoding="utf-8")
    audit_path = tmp_path / "independent-audit.jsonl"
    trace_path = tmp_path / "session-trace.jsonl"
    store = SQLiteStore(tmp_path / "state.db")
    first_tokens = iter(["RAW_EXECUTION_TOKEN_CANARY", "RAW_WORKSPACE_TOKEN_CANARY"])
    task = Task(id="task-1", goal="Update state safely", repository=str(repository))
    response = ModelResponse(
        tool_calls=[
            ToolCall(
                id="replace-state",
                name="replace_text",
                arguments={
                    "path": "state.txt",
                    "old_text": "Before",
                    "new_text": "After",
                },
            )
        ]
    )

    with pytest.raises(KeyboardInterrupt, match="independently audited mutation"):
        AgentRuntime(
            FakeProvider([response]),
            _AuditAfterMutationGateway(repository, audit_path),
            state_store=store,
            ownership_manager=ExecutionOwnershipManager(
                store,
                id_factory=lambda: "execution-before-crash",
                token_factory=lambda: next(first_tokens),
            ),
            owner_id="worker-before-crash",
        ).run(task)

    assert target.read_text(encoding="utf-8") == "After\n"
    interrupted = store.list_effects(task.id)[0]
    assert interrupted.status is EffectStatus.EXECUTING

    second_tokens = iter(["SECOND_EXECUTION_TOKEN", "SECOND_WORKSPACE_TOKEN"])
    recovered = AgentRuntime(
        FakeProvider([ModelResponse(content="Recovered without repeating the write.")]),
        _gateway(repository),
        state_store=store,
        ownership_manager=ExecutionOwnershipManager(
            store,
            id_factory=lambda: "execution-after-crash",
            token_factory=lambda: next(second_tokens),
        ),
        owner_id="worker-after-crash",
    ).resume(store.get_task(task.id), store.get_checkpoint(task.id))

    assert recovered.status is TaskStatus.COMPLETED
    audit_records = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert audit_records == [{"kind": "external_action", "call_id": "replace-state"}]
    assert target.read_text(encoding="utf-8") == "After\n"
    assert store.get_effect(interrupted.id).status is EffectStatus.SUCCEEDED
    assert len(store.list_tool_results(task.id)) == 1

    persisted_task = store.get_task(task.id)
    assert persisted_task.session_id is not None
    journal = store.list_events(persisted_task.session_id)
    committed = [
        event
        for event in journal
        if event.type == "effect.committed" and event.data["effect_id"] == interrupted.id
    ]
    assert len(committed) == 1
    SessionEventExporter(store).export(persisted_task.session_id, trace_path)
    exported = [
        SessionEvent.model_validate_json(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert exported == journal
    serialized_evidence = audit_path.read_text(encoding="utf-8") + trace_path.read_text(
        encoding="utf-8"
    )
    assert "RAW_EXECUTION_TOKEN_CANARY" not in serialized_evidence
    assert "RAW_WORKSPACE_TOKEN_CANARY" not in serialized_evidence
