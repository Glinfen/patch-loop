"""Policy adapters use the real runtime/store without performing external network I/O."""

import json
import multiprocessing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from patchloop.domain import Task, TaskRuntimeCondition, TaskStatus, ToolCall
from patchloop.execution.approvals import ApprovalService
from patchloop.execution.policy import (
    ActionDescriptor,
    ApprovalScopeKind,
    PolicyAction,
    digest,
    normalize_network_target,
)
from patchloop.persistence import SQLiteStore
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.runtime import AgentRuntime
from patchloop.security import RiskLevel
from patchloop.tools import PermissionLevel, Tool, ToolContext, ToolGateway, ToolPolicy


class NetworkInput(BaseModel):
    url: str


class RecordingNetworkTool(Tool):
    name = "test_network"
    description = "Test adapter that records authorized backend calls."
    permission = PermissionLevel.EXECUTE
    input_model = NetworkInput

    def __init__(self) -> None:
        self.calls = 0

    def policy_descriptor(self, arguments: BaseModel, context: ToolContext) -> ActionDescriptor:
        request = NetworkInput.model_validate(arguments)
        return ActionDescriptor(
            action=PolicyAction.NETWORK,
            tool_name=self.name,
            workspace_ref=str(context.repository),
            session_id=context.session_id,
            resources=(normalize_network_target(request.url),),
            arguments_fingerprint=digest(request.model_dump(mode="json")),
            side_effect=True,
            risk=RiskLevel.HIGH,
        )

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        self.calls += 1
        return "recorded"


def response(call_id: str) -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            ToolCall(id=call_id, name="test_network", arguments={"url": "https://example.org/a"})
        ]
    )


def runtime(
    store: SQLiteStore, repository: Path, tool: RecordingNetworkTool, responses: list[ModelResponse]
) -> AgentRuntime:
    return AgentRuntime(
        FakeProvider(responses),
        ToolGateway(
            ToolContext(repository),
            [tool],
            policy=ToolPolicy(
                frozenset({PermissionLevel.EXECUTE}),
                require_plan_for_mutations=False,
                policy_extension=True,
            ),
        ),
        state_store=store,
    )


def _scope_lifecycle_process(
    repository: Path, phase: str, scope: str, task_id: str | None, output: Path
) -> None:
    store = SQLiteStore(repository / "state.db")
    tool = RecordingNetworkTool()
    if phase == "decide":
        assert task_id is not None
        approval = store.list_approvals(task_id)[0]
        resolution = ApprovalService(store).decide_current(
            approval.id,
            approved=True,
            source="second-process",
            scope_kind=ApprovalScopeKind(scope),
            expires_at=datetime.now(UTC) + timedelta(hours=1) if scope == "resource" else None,
        )
        result = {"approval": resolution.approval.model_dump(mode="json")}
    else:
        if phase == "resume":
            assert task_id is not None
            responses = [ModelResponse(content="Done")]
            if scope != "once":
                responses.insert(0, response("second"))
            task = runtime(store, repository, tool, responses).resume(
                store.get_task(task_id), store.get_checkpoint(task_id)
            )
        else:
            task = runtime(
                store, repository, tool, [response(phase), ModelResponse(content="Done")]
            ).run(Task(goal="Cross-process scope lifecycle", repository=str(repository)))
        result = {
            "task": task.model_dump(mode="json"),
            "calls": tool.calls,
            "effects": [effect.model_dump(mode="json") for effect in store.list_effects(task.id)],
        }
    output.write_text(json.dumps(result), encoding="utf-8")


@pytest.mark.parametrize("scope", ["once", "session", "resource"])
def test_scope_approval_and_resume_in_separate_processes(tmp_path: Path, scope: str) -> None:
    def run_phase(phase, task_id=None):
        output = tmp_path / f"{phase}.json"
        process = multiprocessing.get_context("spawn").Process(
            target=_scope_lifecycle_process, args=(tmp_path, phase, scope, task_id, output)
        )
        process.start()
        try:
            process.join(timeout=45)
            assert process.exitcode == 0, f"{phase} failed or timed out: {process.exitcode}"
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        return json.loads(output.read_text(encoding="utf-8"))

    started = run_phase("start")
    assert started["calls"] == 0
    assert started["task"]["runtime_condition"] == "waiting_for_approval"
    task_id = started["task"]["id"]
    decided = run_phase("decide", task_id)["approval"]
    assert decided["scope_kind"] == scope
    resumed = run_phase("resume", task_id)
    assert resumed["task"]["status"] == "completed"
    assert resumed["calls"] == (1 if scope == "once" else 2)
    assert all(effect["approval_consumed"] for effect in resumed["effects"])
    assert all(effect["consumed_grant_id"] == decided["grant_id"] for effect in resumed["effects"])

    # The fourth process verifies that only resource grants cross sessions.
    other = run_phase("new-session")
    assert other["task"]["session_id"] != started["task"]["session_id"]
    assert other["calls"] == (1 if scope == "resource" else 0)
    expected = "ended" if scope == "resource" else "waiting_for_approval"
    assert other["task"]["runtime_condition"] == expected
    store = SQLiteStore(tmp_path / "state.db")
    if scope == "once":
        assert store.get_approval(decided["id"]).status.value == "consumed"
        assert store.list_grants(str(tmp_path)) == []
    else:
        assert store.get_grant(decided["grant_id"]).version == (4 if scope == "resource" else 3)


def test_session_grant_survives_restart_and_is_consumed_for_each_effect(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    tool = RecordingNetworkTool()
    task = runtime(store, tmp_path, tool, [response("first")]).run(
        Task(goal="Record two network actions", repository=str(tmp_path))
    )
    assert task.runtime_condition == TaskRuntimeCondition.WAITING_FOR_APPROVAL
    assert tool.calls == 0
    approval = store.list_approvals(task.id)[0]
    decided, _, _ = ApprovalService(store).decide_current(
        approval.id, approved=True, source="operator", scope_kind=ApprovalScopeKind.SESSION
    )
    reopened = SQLiteStore(store.path)
    finished = runtime(
        reopened, tmp_path, tool, [response("second"), ModelResponse(content="Done")]
    )
    task = finished.resume(reopened.get_task(task.id), reopened.get_checkpoint(task.id))
    assert task.status == TaskStatus.COMPLETED
    assert tool.calls == 2
    assert len(reopened.list_approvals(task.id)) == 1
    assert reopened.get_grant(decided.grant_id or "").version == 3
    effects = reopened.list_effects(task.id)
    assert all(effect.consumed_grant_id == decided.grant_id for effect in effects)


def test_revoked_grant_blocks_even_its_source_approval_before_backend(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    tool = RecordingNetworkTool()
    task = runtime(store, tmp_path, tool, [response("first")]).run(
        Task(goal="Record network action", repository=str(tmp_path))
    )
    service = ApprovalService(store)
    decided, _, _ = service.decide_current(
        store.list_approvals(task.id)[0].id,
        approved=True,
        source="operator",
        scope_kind=ApprovalScopeKind.SESSION,
    )
    service.revoke_grant(decided.grant_id or "", source="operator")
    result = runtime(store, tmp_path, tool, []).resume(
        store.get_task(task.id), store.get_checkpoint(task.id)
    )
    assert result.runtime_condition == TaskRuntimeCondition.WAITING_FOR_APPROVAL
    assert tool.calls == 0
    replacement = store.list_approvals(task.id)[-1]
    assert replacement.id != decided.id
    assert replacement.supersedes_approval_id == decided.id
    service.decide_current(replacement.id, approved=True, source="operator")
    result = runtime(store, tmp_path, tool, [ModelResponse(content="Done")]).resume(
        store.get_task(task.id), store.get_checkpoint(task.id)
    )
    assert result.status == TaskStatus.COMPLETED
    assert tool.calls == 1


def test_changed_network_target_creates_linked_approval_and_revokes_old_grant(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    tool = RecordingNetworkTool()
    task = runtime(store, tmp_path, tool, [response("first")]).run(
        Task(goal="Record network actions", repository=str(tmp_path))
    )
    first = store.list_approvals(task.id)[0]
    decided, _, _ = ApprovalService(store).decide_current(
        first.id, approved=True, source="operator", scope_kind=ApprovalScopeKind.SESSION
    )
    changed = ModelResponse(
        tool_calls=[
            ToolCall(id="changed", name=tool.name, arguments={"url": "https://other.example.org/a"})
        ]
    )
    task = runtime(store, tmp_path, tool, [changed]).resume(
        store.get_task(task.id), store.get_checkpoint(task.id)
    )
    assert task.runtime_condition == TaskRuntimeCondition.WAITING_FOR_APPROVAL
    assert tool.calls == 1
    approvals = store.list_approvals(task.id)
    assert len(approvals) == 2
    assert approvals[1].supersedes_approval_id == first.id
    assert store.get_grant(decided.grant_id or "").status == "revoked"
    assert any(
        event.type == "approval.superseded" for event in store.list_events(task.session_id or "")
    )


def test_resource_grant_reuses_exact_target_across_new_session_after_restart(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime, timedelta

    store = SQLiteStore(tmp_path / "state.db")
    tool = RecordingNetworkTool()
    first = runtime(store, tmp_path, tool, [response("first")]).run(
        Task(goal="First session", repository=str(tmp_path))
    )
    resolution = ApprovalService(store).decide_current(
        store.list_approvals(first.id)[0].id,
        approved=True,
        source="operator",
        scope_kind=ApprovalScopeKind.RESOURCE,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    runtime(store, tmp_path, tool, [ModelResponse(content="Done")]).resume(
        store.get_task(first.id), store.get_checkpoint(first.id)
    )
    reopened = SQLiteStore(store.path)
    second = runtime(
        reopened, tmp_path, tool, [response("second"), ModelResponse(content="Done")]
    ).run(Task(goal="Second session", repository=str(tmp_path)))
    assert second.session_id != first.session_id
    assert second.status is TaskStatus.COMPLETED
    assert tool.calls == 2
    assert reopened.list_approvals(second.id) == []
    assert reopened.list_effects(second.id)[0].consumed_grant_id == resolution.grant.id


@pytest.mark.parametrize("approved_before_migration", [False, True])
def test_legacy_once_approval_regenerates_descriptor_without_creating_grant(
    tmp_path: Path, approved_before_migration: bool
) -> None:
    from patchloop.sqlite_support import connect_write

    store = SQLiteStore(tmp_path / "state.db")
    tool = RecordingNetworkTool()
    task = runtime(store, tmp_path, tool, [response("first")]).run(
        Task(goal="Legacy once request", repository=str(tmp_path))
    )
    if approved_before_migration:
        ApprovalService(store).decide_current(
            store.list_approvals(task.id)[0].id, approved=True, source="legacy-operator"
        )
    effect = store.list_effects(task.id)[0].model_copy(
        update={
            "action_descriptor": None,
            "policy_evaluation": None,
        }
    )
    approval = store.list_approvals(task.id)[0].model_copy(
        update={
            "effect_fingerprint": effect.content_fingerprint(),
        }
    )
    with connect_write(store.path) as connection:
        # Store an actual pre-extension Approval payload, then migrate the DB.
        legacy_payload = approval.model_dump(mode="json")
        for field in (
            "scope_kind",
            "grant_id",
            "expires_at",
            "supersedes_approval_id",
            "decision_reason",
        ):
            legacy_payload.pop(field)
        connection.execute(
            "UPDATE effects SET payload_json = ? WHERE id = ?",
            (effect.model_dump_json(), effect.id),
        )
        connection.execute(
            "UPDATE approvals SET payload_json = ? WHERE id = ?",
            (json.dumps(legacy_payload), approval.id),
        )
        connection.execute("DROP TABLE policy_rules")
        connection.execute("DROP TABLE approval_grants")
        connection.execute(
            "UPDATE patchloop_schema_migrations SET version = 5 WHERE component = 'runtime'"
        )
    store = SQLiteStore(store.path)
    migrated = store.get_approval(approval.id)
    assert migrated.scope_kind is ApprovalScopeKind.ONCE
    assert migrated.grant_id is None
    assert migrated.expires_at is None
    assert migrated.status == approval.status
    if not approved_before_migration:
        ApprovalService(store).decide_current(approval.id, approved=True, source="operator")
    result = runtime(
        SQLiteStore(store.path), tmp_path, tool, [ModelResponse(content="Done")]
    ).resume(store.get_task(task.id), store.get_checkpoint(task.id))
    assert result.status is TaskStatus.COMPLETED
    assert tool.calls == 1
    claimed = store.get_effect(effect.id)
    assert claimed.action_descriptor is not None
    assert claimed.policy_evaluation is not None
    assert claimed.approval_consumed
    assert claimed.consumed_grant_id is None
    assert store.get_approval(approval.id).status.value == "consumed"
    assert store.list_grants(str(tmp_path)) == []


def test_direct_tool_call_cannot_bypass_gateway(tmp_path: Path) -> None:
    import pytest

    tool = RecordingNetworkTool()
    with pytest.raises(PermissionError, match="policy gateway"):
        tool.run(NetworkInput(url="https://example.org/a"), ToolContext(tmp_path))
    assert tool.calls == 0


def test_policy_version_drift_requires_new_approval_before_backend(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "state.db")
    tool = RecordingNetworkTool()
    task = runtime(store, tmp_path, tool, [response("first")]).run(
        Task(goal="Version drift", repository=str(tmp_path))
    )
    decided = (
        ApprovalService(store)
        .decide_current(
            store.list_approvals(task.id)[0].id,
            approved=True,
            source="operator",
            scope_kind=ApprovalScopeKind.SESSION,
        )
        .approval
    )
    restarted = runtime(SQLiteStore(store.path), tmp_path, tool, [])
    restarted.gateway.policy.configuration_fingerprint = "changed-policy"
    result = restarted.resume(store.get_task(task.id), store.get_checkpoint(task.id))
    assert result.runtime_condition == TaskRuntimeCondition.WAITING_FOR_APPROVAL
    assert tool.calls == 0
    approvals = store.list_approvals(task.id)
    replacement = next(a for a in approvals if a.supersedes_approval_id == decided.id)
    assert replacement.policy_version == restarted.gateway.policy.version
    assert store.get_grant(decided.grant_id).status == "revoked"


def test_expired_resource_grant_renews_before_backend(tmp_path: Path, monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    import patchloop.execution.models as models
    import patchloop.execution.policy as policy

    store = SQLiteStore(tmp_path / "state.db")
    tool = RecordingNetworkTool()
    task = runtime(store, tmp_path, tool, [response("first")]).run(
        Task(goal="Expiry", repository=str(tmp_path))
    )
    expires = datetime.now(UTC) + timedelta(hours=1)
    decided = (
        ApprovalService(store)
        .decide_current(
            store.list_approvals(task.id)[0].id,
            approved=True,
            source="operator",
            scope_kind=ApprovalScopeKind.RESOURCE,
            expires_at=expires,
        )
        .approval
    )

    class FutureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return expires + timedelta(seconds=1)

    monkeypatch.setattr(policy, "datetime", FutureClock)
    monkeypatch.setattr(models, "_now", lambda: expires + timedelta(seconds=1))
    result = runtime(SQLiteStore(store.path), tmp_path, tool, []).resume(
        store.get_task(task.id), store.get_checkpoint(task.id)
    )
    assert result.runtime_condition == TaskRuntimeCondition.WAITING_FOR_APPROVAL
    assert tool.calls == 0
    assert store.get_grant(decided.grant_id).status == "expired"
    assert any(a.supersedes_approval_id == decided.id for a in store.list_approvals(task.id))


@pytest.mark.parametrize(
    "action,initial,changed",
    [
        (
            PolicyAction.DEPENDENCY_INSTALL,
            {"manager": "pip", "name": "demo", "version": "1", "source": "https://example.org"},
            {"manager": "pip", "name": "demo", "version": "2", "source": "https://example.org"},
        ),
        (
            PolicyAction.DEPENDENCY_INSTALL,
            {"manager": "pip", "name": "demo", "version": "1", "source": "https://example.org"},
            {
                "manager": "pip",
                "name": "demo",
                "version": "1",
                "source": "https://other.example.org",
            },
        ),
        (
            PolicyAction.SKILL_EXECUTE,
            {"registry": "project", "id": "demo", "version": "1"},
            {"registry": "project", "id": "demo", "version": "2"},
        ),
    ],
)
def test_changed_package_or_skill_requires_linked_new_approval(tmp_path, action, initial, changed):
    from patchloop.evaluation.policy_audit import _RecordingAdapter

    store = SQLiteStore(tmp_path / "state.db")
    adapter = _RecordingAdapter(action)

    def create_runtime(responses):
        return AgentRuntime(
            FakeProvider(responses),
            ToolGateway(
                ToolContext(tmp_path),
                [adapter],
                policy=ToolPolicy(
                    frozenset({PermissionLevel.EXECUTE}),
                    require_plan_for_mutations=False,
                    policy_extension=True,
                ),
            ),
            state_store=store,
        )

    first_call = ModelResponse(
        tool_calls=[ToolCall(id="first", name=adapter.name, arguments=initial)]
    )
    task = create_runtime([first_call]).run(
        Task(goal="Exact external scope", repository=str(tmp_path))
    )
    original = store.list_approvals(task.id)[0]
    decided = (
        ApprovalService(store)
        .decide_current(
            original.id,
            approved=True,
            source="operator",
            scope_kind=ApprovalScopeKind.SESSION,
        )
        .approval
    )
    changed_call = ModelResponse(
        tool_calls=[ToolCall(id="changed", name=adapter.name, arguments=changed)]
    )
    result = create_runtime([changed_call]).resume(
        store.get_task(task.id), store.get_checkpoint(task.id)
    )
    assert result.runtime_condition == TaskRuntimeCondition.WAITING_FOR_APPROVAL
    assert adapter.calls == 1
    assert any(a.supersedes_approval_id == original.id for a in store.list_approvals(task.id))
    assert store.get_grant(decided.grant_id).status == "revoked"
