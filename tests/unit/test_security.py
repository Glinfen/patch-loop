import json
from pathlib import Path

from patchloop.domain import ErrorKind, Task, ToolCall, ToolResult
from patchloop.events import Event, EventLogger
from patchloop.persistence import SQLiteStore
from patchloop.security import RiskLevel, SecretRedactor
from patchloop.tools import (
    PermissionLevel,
    ReplaceTextTool,
    RunCommandTool,
    ToolContext,
    ToolGateway,
    ToolPolicy,
)


def test_secret_redactor_covers_api_keys_bearer_tokens_and_named_fields() -> None:
    redactor = SecretRedactor()
    source = (
        "sk-abcdefghijklmnopqrstuvwxyz123456 "
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz "
        'password="do-not-store" api_key=also-secret'
    )

    result = redactor.redact_text(source)

    assert "abcdefghijklmnopqrstuvwxyz" not in result
    assert "do-not-store" not in result
    assert "also-secret" not in result
    assert result.count("[REDACTED]") == 4


def test_trace_and_sqlite_persistence_redact_secrets(tmp_path: Path) -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    logger = EventLogger(tmp_path / "trace.jsonl")
    logger.emit(Event(type="test", task_id="task-1", data={"token": secret}))
    logger.emit(Event(type="test.done", task_id="task-1", data={"password": "hidden"}))

    events = logger.read()
    assert [event.sequence for event in events] == [1, 2]
    assert all(event.trace_id == "task-1" for event in events)
    assert secret not in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert events[0].data["token"] == "[REDACTED]"
    assert events[1].data["password"] == "[REDACTED]"

    store = SQLiteStore(tmp_path / "patchloop.db")
    task = Task(id="task-1", goal=f"Inspect api_key={secret}", repository=str(tmp_path))
    store.save_task(task)
    call = ToolCall(id="call-1", name="read_file")
    result = ToolResult(
        call_id=call.id,
        tool_name=call.name,
        success=True,
        output=f"Bearer {secret}",
    )
    store.record_tool_call(task.id, call, result)

    assert secret not in store.get_task(task.id).goal
    assert secret not in store.get_tool_result(task.id, call.id).output  # type: ignore[union-attr]


def test_medium_risk_write_requires_and_records_approval(tmp_path: Path) -> None:
    target = tmp_path / "README.md"
    target.write_text("old", encoding="utf-8")
    logger = EventLogger(tmp_path / "trace.jsonl")
    policy = ToolPolicy(
        frozenset({PermissionLevel.WRITE}),
        require_plan_for_mutations=False,
        approval_threshold=RiskLevel.MEDIUM,
        approval_handler=lambda request: False,
    )
    gateway = ToolGateway(ToolContext(tmp_path), [ReplaceTextTool()], logger, policy)
    call = ToolCall(
        name="replace_text",
        arguments={"path": "README.md", "old_text": "old", "new_text": "new"},
    )

    denied = gateway.execute("task-1", call)

    assert denied.error_kind is ErrorKind.PERMISSION_DENIED
    assert target.read_text(encoding="utf-8") == "old"
    decision = next(event for event in logger.read() if event.type == "security.decision")
    assert decision.data["assessment"]["risk"] == "medium"
    assert decision.data["assessment"]["approval_required"] is True
    assert decision.data["assessment"]["allowed"] is False


def test_network_capable_command_is_denied_before_execution(tmp_path: Path) -> None:
    gateway = ToolGateway(
        ToolContext(tmp_path),
        [RunCommandTool()],
        policy=ToolPolicy(
            frozenset({PermissionLevel.EXECUTE}),
            require_plan_for_mutations=False,
        ),
    )

    result = gateway.execute(
        "task-1",
        ToolCall(name="run_command", arguments={"command": ["curl", "https://example.com"]}),
    )

    assert result.error_kind is ErrorKind.PERMISSION_DENIED
    assert "network-capable" in result.output


def test_security_decision_arguments_are_valid_json(tmp_path: Path) -> None:
    logger = EventLogger(tmp_path / "trace.jsonl")
    gateway = ToolGateway(
        ToolContext(tmp_path),
        [RunCommandTool()],
        logger,
        ToolPolicy(
            frozenset({PermissionLevel.EXECUTE}),
            require_plan_for_mutations=False,
        ),
    )

    gateway.execute(
        "task-1",
        ToolCall(name="run_command", arguments={"command": ["git", "diff", "|"]}),
    )

    payload = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert payload["type"] == "security.decision"
    assert payload["data"]["assessment"]["risk"] == "critical"
