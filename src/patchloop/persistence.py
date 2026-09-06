"""SQLite persistence for runtime state, artifacts, and layered memory."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from patchloop.domain import (
    AgentStep,
    Plan,
    SessionStatus,
    Task,
    TaskOutcome,
    TaskRuntimeCondition,
    TaskStatus,
    ToolCall,
    ToolResult,
)
from patchloop.events import SessionEvent, effect_commit_event_id, journal_event_id
from patchloop.execution.models import (
    Approval,
    ApprovalStatus,
    ControlKind,
    ControlRequest,
    ControlStatus,
    Effect,
    EffectStatus,
    Execution,
    ExecutionStatus,
    RecoveryDisposition,
    RecoveryDispositionKind,
)
from patchloop.memory.episodic import EpisodicMemorySnapshot
from patchloop.memory.manager import MemoryManagerSnapshot
from patchloop.memory.store import SQLiteMemoryStore, initialize_memory_schema
from patchloop.memory.working import WorkingMemorySnapshot
from patchloop.persistence_contracts import (
    ApprovalConflict,
    LeaseConflict,
    LeaseGuard,
    LeaseLost,
    RecoveryRequired,
    StaleVersion,
    SubmissionConflict,
)
from patchloop.prompt_cache import (
    CacheDiagnosticsSnapshot,
    CacheEpochSnapshot,
    MemoryPublicationSnapshot,
)
from patchloop.providers.base import ModelMessage, ToolSpec
from patchloop.security import SecretRedactor, persist_tool_arguments
from patchloop.session.models import Session, SessionCheckpoint, Turn
from patchloop.sqlite_support import (
    MigrationBackup,
    connect,
    connect_write,
    create_migration_backup,
    has_runtime_data,
    open_connection,
    runtime_schema_version,
)
from patchloop.storage import TaskNotFoundError

RUNTIME_SCHEMA_VERSION = 2
_RUNTIME_TABLES = {
    "tasks",
    "agent_steps",
    "tool_calls",
    "checkpoints",
    "artifacts",
    "sessions",
    "turns",
    "executions",
    "effects",
    "approvals",
    "control_requests",
    "recovery_dispositions",
    "session_events",
    "workspace_leases",
}

_RUNTIME_TASK_COLUMNS = {"session_id", "outcome", "runtime_condition", "version"}

_RUNTIME_MIGRATION_V1 = (
    # Legacy runtime tables keep their historical shape; a fresh database
    # creates them here and the session projection columns are appended below,
    # so legacy v0 databases and fresh databases follow the same statements.
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_steps (
        task_id TEXT NOT NULL,
        step_index INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (task_id, step_index),
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tool_calls (
        task_id TEXT NOT NULL,
        call_id TEXT NOT NULL,
        call_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        PRIMARY KEY (task_id, call_id),
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checkpoints (
        task_id TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        task_id TEXT NOT NULL,
        name TEXT NOT NULL,
        path TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (task_id, name),
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        workspace_ref TEXT NOT NULL,
        status TEXT NOT NULL,
        active_task_id TEXT,
        config_version TEXT NOT NULL,
        version INTEGER NOT NULL,
        event_sequence INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        FOREIGN KEY (active_task_id) REFERENCES tasks(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS turns (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        task_id TEXT,
        sequence INTEGER NOT NULL,
        client_submission_id TEXT,
        created_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        UNIQUE (session_id, sequence),
        UNIQUE (session_id, client_submission_id),
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS executions (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        lease_token TEXT NOT NULL,
        generation INTEGER NOT NULL,
        lease_expires_at TEXT NOT NULL,
        status TEXT NOT NULL,
        version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS effects (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        step_id TEXT NOT NULL,
        batch_position INTEGER NOT NULL,
        provider_call_id TEXT NOT NULL,
        retry_of_effect_id TEXT,
        status TEXT NOT NULL,
        approval_id TEXT,
        version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        UNIQUE (task_id, step_id, batch_position),
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
        FOREIGN KEY (retry_of_effect_id) REFERENCES effects(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS approvals (
        id TEXT PRIMARY KEY,
        effect_id TEXT NOT NULL,
        status TEXT NOT NULL,
        decision_source TEXT,
        decided_at TEXT,
        version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        UNIQUE (effect_id),
        FOREIGN KEY (effect_id) REFERENCES effects(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS control_requests (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        execution_id TEXT,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        version INTEGER NOT NULL,
        requested_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
        FOREIGN KEY (execution_id) REFERENCES executions(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recovery_dispositions (
        id TEXT PRIMARY KEY,
        unknown_effect_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        retry_effect_id TEXT,
        decision_source TEXT NOT NULL,
        created_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        UNIQUE (unknown_effect_id),
        FOREIGN KEY (unknown_effect_id) REFERENCES effects(id) ON DELETE CASCADE,
        FOREIGN KEY (retry_effect_id) REFERENCES effects(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_events (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        type TEXT NOT NULL,
        task_id TEXT,
        trace_id TEXT,
        created_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        UNIQUE (session_id, sequence),
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workspace_leases (
        workspace_id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        token TEXT NOT NULL,
        generation INTEGER NOT NULL,
        lease_expires_at TEXT NOT NULL,
        acquired_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # Task projections are appended after sessions exists so the reference is
    # valid; both fresh and legacy v0 databases reach this point without them.
    "ALTER TABLE tasks ADD COLUMN session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL",
    "ALTER TABLE tasks ADD COLUMN outcome TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE tasks ADD COLUMN runtime_condition TEXT NOT NULL DEFAULT 'idle'",
    "ALTER TABLE tasks ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
    # Legacy rows keep their payload as the source of truth; the projection
    # columns are backfilled from the legacy status following the SRF-01
    # compatibility projection before any session mapping happens (SRF-02).
    """
    UPDATE tasks SET runtime_condition = 'running'
    WHERE status = 'running'
    """,
    """
    UPDATE tasks SET outcome = 'completed', runtime_condition = 'ended'
    WHERE status = 'completed'
    """,
    """
    UPDATE tasks SET outcome = 'failed', runtime_condition = 'ended'
    WHERE status = 'failed'
    """,
    """
    UPDATE tasks SET outcome = 'cancelled', runtime_condition = 'ended'
    WHERE status = 'cancelled'
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace_ref)",
    "CREATE INDEX IF NOT EXISTS idx_executions_task ON executions(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_effects_task ON effects(task_id)",
)


class RuntimeSchemaError(RuntimeError):
    """Raised when the runtime schema cannot be migrated or is unusable."""


def initialize_runtime_schema(connection: sqlite3.Connection) -> None:
    """Apply runtime schema migrations atomically, before memory migrations."""

    connection.execute("SAVEPOINT patchloop_runtime_migration")
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS patchloop_schema_migrations (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        row = connection.execute(
            "SELECT version FROM patchloop_schema_migrations WHERE component = 'runtime'"
        ).fetchone()
        version = 0 if row is None else int(row[0])
        if version > RUNTIME_SCHEMA_VERSION:
            raise RuntimeSchemaError(
                f"runtime schema {version} is newer than supported {RUNTIME_SCHEMA_VERSION}"
            )
        if version < 1:
            for statement in _RUNTIME_MIGRATION_V1:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO patchloop_schema_migrations (component, version, updated_at)
                VALUES ('runtime', ?, ?)
                """,
                (1, datetime.now(UTC).isoformat()),
            )
            version = 1
        if version < 2:
            _migrate_legacy_runtime_v0(connection)
            connection.execute(
                """
                UPDATE patchloop_schema_migrations
                SET version = ?, updated_at = ? WHERE component = 'runtime'
                """,
                (2, datetime.now(UTC).isoformat()),
            )
        tables = {
            str(table[0])
            for table in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not _RUNTIME_TABLES.issubset(tables):
            missing = ", ".join(sorted(_RUNTIME_TABLES - tables))
            raise RuntimeSchemaError(f"runtime schema is incomplete; missing: {missing}")
        task_columns = {
            str(column[1]) for column in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if not _RUNTIME_TASK_COLUMNS.issubset(task_columns):
            missing = ", ".join(sorted(_RUNTIME_TASK_COLUMNS - task_columns))
            raise RuntimeSchemaError(
                f"runtime tasks table is incomplete; missing columns: {missing}"
            )
        connection.execute("RELEASE SAVEPOINT patchloop_runtime_migration")
    except Exception as exc:
        connection.execute("ROLLBACK TO SAVEPOINT patchloop_runtime_migration")
        connection.execute("RELEASE SAVEPOINT patchloop_runtime_migration")
        if isinstance(exc, RuntimeSchemaError):
            raise
        raise RuntimeSchemaError(f"runtime schema migration failed: {exc}") from exc


def _migrate_legacy_runtime_v0(connection: sqlite3.Connection) -> None:
    """Map each pre-Session Task to one deterministic legacy Session."""

    rows = connection.execute(
        "SELECT id, payload_json FROM tasks WHERE session_id IS NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        task = Task.model_validate_json(row["payload_json"])
        session_id = f"legacy-session-{task.id}"
        condition = {
            TaskStatus.CREATED: TaskRuntimeCondition.IDLE,
            TaskStatus.RUNNING: TaskRuntimeCondition.RECOVERY_REQUIRED,
            TaskStatus.COMPLETED: TaskRuntimeCondition.ENDED,
            TaskStatus.FAILED: TaskRuntimeCondition.ENDED,
            TaskStatus.CANCELLED: TaskRuntimeCondition.ENDED,
        }[task.status]
        migrated_task = task.model_copy(
            update={
                "session_id": session_id,
                "runtime_condition": condition,
                "version": 1,
            }
        )
        effects = _legacy_effects(connection, migrated_task)
        session = Session(
            id=session_id,
            workspace_ref=migrated_task.repository or ".",
            active_task_id=(
                migrated_task.id if migrated_task.outcome is TaskOutcome.ACTIVE else None
            ),
            event_sequence=1,
            created_at=migrated_task.created_at,
            updated_at=migrated_task.updated_at,
        )
        connection.execute(
            """
            INSERT INTO sessions (
                id, workspace_ref, status, active_task_id, config_version,
                version, event_sequence, created_at, updated_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.workspace_ref,
                session.status.value,
                session.active_task_id,
                session.config_version,
                session.version,
                session.event_sequence,
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
                session.model_dump_json(),
            ),
        )
        connection.execute(
            """
            UPDATE tasks SET
                session_id = ?, outcome = ?, runtime_condition = ?,
                version = ?, payload_json = ?
            WHERE id = ?
            """,
            (
                session.id,
                migrated_task.outcome.value,
                migrated_task.runtime_condition.value,
                migrated_task.version,
                migrated_task.model_dump_json(),
                migrated_task.id,
            ),
        )
        for effect in effects:
            _insert_legacy_effect(connection, effect)
        _adapt_legacy_checkpoint(connection, migrated_task.id, session.id, session.event_sequence)
        event = SessionEvent(
            id=journal_event_id("legacy.session.migrated", migrated_task.id, 1),
            session_id=session.id,
            task_id=migrated_task.id,
            trace_id=migrated_task.id,
            sequence=1,
            type="legacy.session.migrated",
            data={
                "legacy_task_id": migrated_task.id,
                "legacy_status": migrated_task.status.value,
                "effect_ids": [effect.id for effect in effects],
                "requires_recovery_review": condition is TaskRuntimeCondition.RECOVERY_REQUIRED,
            },
            timestamp=migrated_task.updated_at,
        )
        connection.execute(
            """
            INSERT INTO session_events (
                id, session_id, sequence, type, task_id, trace_id,
                created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.session_id,
                event.sequence,
                event.type,
                event.task_id,
                event.trace_id,
                event.timestamp.isoformat(),
                event.model_dump_json(),
            ),
        )


def _legacy_effects(connection: sqlite3.Connection, task: Task) -> list[Effect]:
    step_rows = connection.execute(
        "SELECT payload_json FROM agent_steps WHERE task_id = ? ORDER BY step_index",
        (task.id,),
    ).fetchall()
    call_locations: dict[str, tuple[str, int]] = {}
    for step_row in step_rows:
        step = AgentStep.model_validate_json(step_row["payload_json"])
        for position, result in enumerate(step.tool_results):
            call_locations[result.call_id] = (step.id, position)
    call_rows = connection.execute(
        """
        SELECT call_id, call_json, result_json, completed_at
        FROM tool_calls WHERE task_id = ? ORDER BY completed_at, call_id
        """,
        (task.id,),
    ).fetchall()
    effects: list[Effect] = []
    redactor = SecretRedactor()
    for position, call_row in enumerate(call_rows):
        call = ToolCall.model_validate_json(call_row["call_json"])
        result = ToolResult.model_validate_json(call_row["result_json"])
        step_id, batch_position = call_locations.get(call.id, (f"legacy-step-{call.id}", position))
        completed_at = datetime.fromisoformat(str(call_row["completed_at"]).replace("Z", "+00:00"))
        persisted_arguments = persist_tool_arguments(call.arguments, redactor=redactor)
        effects.append(
            Effect(
                id=journal_event_id("legacy.effect", f"{task.id}:{call.id}", 1),
                task_id=task.id,
                step_id=step_id,
                batch_position=batch_position,
                provider_call_id=call.id,
                tool_name=call.name,
                action_kind="legacy",
                arguments_summary=persisted_arguments.arguments,
                credential_bindings=persisted_arguments.credential_bindings,
                redacted_argument_paths=persisted_arguments.redacted_paths,
                status=EffectStatus.SUCCEEDED if result.success else EffectStatus.FAILED,
                result_ref=f"tool-result:{task.id}:{call.id}",
                observation_ref=f"tool-result:{task.id}:{call.id}",
                created_at=completed_at,
                updated_at=completed_at,
            )
        )
    return effects


def _insert_legacy_effect(connection: sqlite3.Connection, effect: Effect) -> None:
    connection.execute(
        """
        INSERT INTO effects (
            id, task_id, step_id, batch_position, provider_call_id,
            retry_of_effect_id, status, approval_id, version,
            created_at, updated_at, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            effect.id,
            effect.task_id,
            effect.step_id,
            effect.batch_position,
            effect.provider_call_id,
            effect.retry_of_effect_id,
            effect.status.value,
            effect.approval_id,
            effect.version,
            effect.created_at.isoformat(),
            effect.updated_at.isoformat(),
            effect.model_dump_json(),
        ),
    )


def _adapt_legacy_checkpoint(
    connection: sqlite3.Connection, task_id: str, session_id: str, event_sequence: int
) -> None:
    row = connection.execute(
        "SELECT payload_json FROM checkpoints WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return
    payload = json.loads(row["payload_json"])
    payload.update(
        {
            "schema_version": "1.0",
            "session_id": session_id,
            "turn_id": None,
            "consumed_input_sequence": 0,
            "event_sequence": event_sequence,
            "pending_effect_ids": [],
        }
    )
    connection.execute(
        "UPDATE checkpoints SET payload_json = ? WHERE task_id = ?",
        (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), task_id),
    )


class RuntimeCheckpoint(BaseModel):
    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")
    task_id: str
    session_id: str | None = None
    turn_id: str | None = None
    consumed_input_sequence: int = Field(default=0, ge=0)
    event_sequence: int = Field(default=0, ge=0)
    pending_effect_ids: list[str] = Field(default_factory=list)
    next_step_index: int = Field(ge=0)
    messages: list[ModelMessage]
    tool_specifications: list[ToolSpec] | None = None
    prompt_prefix_message_count: int = Field(default=2, ge=2)
    cache_epoch_state: CacheEpochSnapshot | None = None
    memory_publication_state: MemoryPublicationSnapshot | None = None
    plan: Plan | None = None
    requires_replan: bool = False
    replan_count: int = Field(default=0, ge=0)
    change_snapshot: dict[str, str | None] = Field(default_factory=dict)
    tool_history: list[ToolResult] = Field(default_factory=list)
    previous_fingerprint: str | None = None
    repeated_actions: int = Field(default=0, ge=0)
    repeated_errors: dict[str, int] = Field(default_factory=dict)
    tool_failures: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    cache_hit_tokens: int = Field(default=0, ge=0)
    cache_miss_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cache_usage_reported_calls: int = Field(default=0, ge=0)
    cache_usage_unreported_calls: int = Field(default=0, ge=0)
    cache_usage_inconsistent_calls: int = Field(default=0, ge=0)
    cache_write_reported_calls: int = Field(default=0, ge=0)
    cache_diagnostics: CacheDiagnosticsSnapshot | None = None
    elapsed_seconds: float = Field(default=0.0, ge=0)
    context_windows: int = Field(default=0, ge=0)
    context_compactions: int = Field(default=0, ge=0)
    max_context_tokens_used: int = Field(default=0, ge=0)
    truncated_tool_outputs: int = Field(default=0, ge=0)
    working_memory: WorkingMemorySnapshot | None = None
    episodic_memory: EpisodicMemorySnapshot | None = None
    semantic_facts_created: int = Field(default=0, ge=0)
    semantic_facts_superseded: int = Field(default=0, ge=0)
    semantic_conflicts_rejected: int = Field(default=0, ge=0)
    semantic_duplicates_suppressed: int = Field(default=0, ge=0)
    memory_retrievals: int = Field(default=0, ge=0)
    memory_retrieval_hits: int = Field(default=0, ge=0)
    memory_retrieval_tokens: int = Field(default=0, ge=0)
    memory_stale_hits: int = Field(default=0, ge=0)
    memory_security_filters: int = Field(default=0, ge=0)
    max_memory_context_tokens_used: int = Field(default=0, ge=0)
    max_memory_context_occupancy: float = Field(default=0.0, ge=0.0, le=1.0)
    memory_manager: MemoryManagerSnapshot | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CheckpointSchemaError(RuntimeError):
    """Raised when a persisted checkpoint cannot be safely adapted."""


def _adapt_checkpoint_json[ModelT: BaseModel](
    payload_json: str, model_type: type[ModelT]
) -> ModelT:
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise CheckpointSchemaError("checkpoint payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CheckpointSchemaError("checkpoint payload must be a JSON object")
    schema_version = payload.get("schema_version", "1.0")
    if schema_version != "1.0":
        raise CheckpointSchemaError(f"checkpoint schema {schema_version} is not supported")
    payload["schema_version"] = "1.0"
    return model_type.model_validate(payload)


class SQLiteStore:
    def __init__(self, path: Path, redactor: SecretRedactor | None = None) -> None:
        self.path = path
        self.redactor = redactor or SecretRedactor()
        self.last_migration_backup: MigrationBackup | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self.memory = SQLiteMemoryStore(self.path, self.redactor)

    def _redacted_json(self, model: BaseModel) -> str:
        payload = self.redactor.redact(model.model_dump(mode="json"))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _initialize(self) -> None:
        connection = open_connection(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            source_version = runtime_schema_version(connection)
            if source_version < RUNTIME_SCHEMA_VERSION and has_runtime_data(connection):
                with sqlite3.connect(self.path) as snapshot_source:
                    self.last_migration_backup = create_migration_backup(
                        snapshot_source,
                        self.path,
                        source_runtime_schema=source_version,
                        target_runtime_schema=RUNTIME_SCHEMA_VERSION,
                    )
            connection.execute("SAVEPOINT patchloop_schema_upgrade")
            try:
                initialize_runtime_schema(connection)
                initialize_memory_schema(connection)
            except Exception:
                connection.execute("ROLLBACK TO SAVEPOINT patchloop_schema_upgrade")
                connection.execute("RELEASE SAVEPOINT patchloop_schema_upgrade")
                raise
            connection.execute("RELEASE SAVEPOINT patchloop_schema_upgrade")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_task(self, task: Task) -> None:
        with connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, status, payload_json, updated_at,
                    session_id, outcome, runtime_condition, version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at,
                    session_id = excluded.session_id,
                    outcome = excluded.outcome,
                    runtime_condition = excluded.runtime_condition,
                    version = excluded.version
                """,
                (
                    task.id,
                    task.status,
                    self._redacted_json(task),
                    task.updated_at.isoformat(),
                    task.session_id,
                    task.outcome.value,
                    task.runtime_condition.value,
                    task.version,
                ),
            )

    def get_task(self, task_id: str) -> Task:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFoundError(task_id)
        return Task.model_validate_json(row["payload_json"])

    def record_step(self, step: AgentStep) -> None:
        with connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO agent_steps (task_id, step_index, payload_json)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id, step_index) DO UPDATE SET
                    payload_json = excluded.payload_json
                """,
                (step.task_id, step.index, self._redacted_json(step)),
            )

    def list_steps(self, task_id: str) -> list[AgentStep]:
        with connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM agent_steps
                WHERE task_id = ? ORDER BY step_index
                """,
                (task_id,),
            ).fetchall()
        return [AgentStep.model_validate_json(row["payload_json"]) for row in rows]

    def record_tool_call(self, task_id: str, call: ToolCall, result: ToolResult) -> None:
        with connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO tool_calls
                    (call_id, task_id, call_json, result_json, completed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id, call_id) DO NOTHING
                """,
                (
                    call.id,
                    task_id,
                    self._redacted_json(call),
                    self._redacted_json(result),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get_tool_result(self, task_id: str, call_id: str) -> ToolResult | None:
        with connect(self.path) as connection:
            row = connection.execute(
                """
                SELECT result_json FROM tool_calls
                WHERE task_id = ? AND call_id = ?
                """,
                (task_id, call_id),
            ).fetchone()
        return None if row is None else ToolResult.model_validate_json(row["result_json"])

    def list_tool_results(self, task_id: str) -> list[ToolResult]:
        with connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT result_json FROM tool_calls
                WHERE task_id = ? ORDER BY completed_at, call_id
                """,
                (task_id,),
            ).fetchall()
        return [ToolResult.model_validate_json(row["result_json"]) for row in rows]

    def save_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        with connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO checkpoints (task_id, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    checkpoint.task_id,
                    self._redacted_json(checkpoint),
                    checkpoint.updated_at.isoformat(),
                ),
            )

    def get_checkpoint(self, task_id: str) -> RuntimeCheckpoint:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM checkpoints WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"checkpoint:{task_id}")
        return _adapt_checkpoint_json(row["payload_json"], RuntimeCheckpoint)

    def record_artifact(self, task_id: str, path: Path) -> None:
        with connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO artifacts (task_id, name, path, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id, name) DO UPDATE SET
                    path = excluded.path,
                    created_at = excluded.created_at
                """,
                (task_id, path.name, str(path), datetime.now(UTC).isoformat()),
            )

    def list_artifacts(self, task_id: str) -> list[Path]:
        with connect(self.path) as connection:
            rows = connection.execute(
                "SELECT path FROM artifacts WHERE task_id = ? ORDER BY name",
                (task_id,),
            ).fetchall()
        return [Path(row["path"]) for row in rows]

    def cancel_task(self, task_id: str) -> Task:
        task = self.get_task(task_id)
        if task.status in {TaskStatus.CREATED, TaskStatus.RUNNING}:
            task.transition(TaskStatus.CANCELLED)
            self.save_task(task)
        return task

    def is_cancelled(self, task_id: str) -> bool:
        try:
            return self.get_task(task_id).status is TaskStatus.CANCELLED
        except TaskNotFoundError:
            return False

    def delete_task(self, task_id: str) -> None:
        """Delete runtime and memory state through SQLite foreign-key cascades."""

        with connect(self.path) as connection:
            cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cursor.rowcount == 0:
            raise TaskNotFoundError(task_id)

    # -- Session store surface (SRF-02) -------------------------------------

    def create_session(self, session: Session) -> Session:
        safe = self._redacted_session(session)
        with connect_write(self.path) as connection:
            if self._session_row(connection, safe.id) is not None:
                raise ValueError(f"session already exists: {safe.id}")
            self._insert_session_row(connection, safe)
            _, safe = self._journal(
                connection,
                safe,
                event_id=journal_event_id("session.created", safe.id, safe.version),
                event_type="session.created",
                data={"workspace_ref": safe.workspace_ref},
            )
            return safe

    def list_sessions(self, workspace_ref: str | None = None) -> list[Session]:
        query = "SELECT payload_json FROM sessions"
        parameters: tuple[str, ...] = ()
        if workspace_ref is not None:
            query += " WHERE workspace_ref = ?"
            parameters = (workspace_ref,)
        query += " ORDER BY created_at, id"
        with connect(self.path) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [Session.model_validate_json(row["payload_json"]) for row in rows]

    def get_session(self, session_id: str) -> Session:
        with connect(self.path) as connection:
            session = self._session_row(connection, session_id)
        if session is None:
            raise KeyError(f"session not found: {session_id}")
        return session

    def close_session(self, session_id: str, *, expected_version: int) -> Session:
        with connect_write(self.path) as connection:
            session = self._require_session(connection, session_id)
            if session.version != expected_version:
                raise StaleVersion(session_id, expected_version, session.version)
            if session.active_task_id is not None:
                raise ValueError("cannot close a session with an active task")
            closed = session.model_copy(
                update={
                    "status": SessionStatus.CLOSED,
                    "version": session.version + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            _, closed = self._journal(
                connection,
                closed,
                event_id=journal_event_id("session.closed", closed.id, closed.version),
                event_type="session.closed",
            )
            return closed

    def append_turn(self, turn: Turn, *, expected_version: int | None = None) -> Turn:
        safe = self._redacted_turn(turn)
        with connect_write(self.path) as connection:
            session = self._require_session(connection, safe.session_id)
            if session.status is SessionStatus.CLOSED:
                raise ValueError("cannot append a turn to a closed session")
            if safe.client_submission_id is not None:
                existing = self._turn_by_submission(
                    connection, safe.session_id, safe.client_submission_id
                )
                if existing is not None:
                    if self._same_turn_submission(existing, safe):
                        return existing
                    raise SubmissionConflict(session.id, safe.client_submission_id)
            if expected_version is not None and session.version != expected_version:
                raise StaleVersion(session.id, expected_version, session.version)
            if safe.task_id is not None:
                task = self._require_task(connection, safe.task_id)
                if task.session_id != session.id:
                    raise ValueError("turn task does not belong to its session")
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM turns WHERE session_id = ?",
                    (safe.session_id,),
                ).fetchone()[0]
            )
            assigned = safe.model_copy(
                update={
                    "sequence": sequence,
                    "task_id": safe.task_id or session.active_task_id,
                }
            )
            connection.execute(
                """
                INSERT INTO turns (
                    id, session_id, task_id, sequence, client_submission_id,
                    created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assigned.id,
                    assigned.session_id,
                    assigned.task_id,
                    assigned.sequence,
                    assigned.client_submission_id,
                    assigned.created_at.isoformat(),
                    self._redacted_json(assigned),
                ),
            )
            _, _ = self._journal(
                connection,
                session.model_copy(
                    update={
                        "version": session.version + 1,
                        "updated_at": datetime.now(UTC),
                    }
                ),
                event_id=journal_event_id("turn.appended", assigned.id, assigned.sequence),
                event_type="turn.appended",
                task_id=assigned.task_id,
                data={"turn_id": assigned.id, "role": assigned.role.value},
            )
            return assigned

    def list_turns(self, session_id: str, *, after_sequence: int = 0) -> list[Turn]:
        with connect(self.path) as connection:
            self._require_session(connection, session_id)
            rows = connection.execute(
                """
                SELECT payload_json FROM turns
                WHERE session_id = ? AND sequence > ? ORDER BY sequence
                """,
                (session_id, after_sequence),
            ).fetchall()
        return [Turn.model_validate_json(row["payload_json"]) for row in rows]

    def start_task(self, session_id: str, task: Task, *, expected_version: int) -> Task:
        safe = self._redacted_task(task)
        with connect_write(self.path) as connection:
            session = self._require_session(connection, session_id)
            if session.version != expected_version:
                raise StaleVersion(session_id, expected_version, session.version)
            if session.status is SessionStatus.CLOSED:
                raise ValueError("cannot start a task in a closed session")
            if session.active_task_id is not None:
                raise LeaseConflict(session_id, session.active_task_id)
            if safe.outcome is not TaskOutcome.ACTIVE:
                raise ValueError("a new session task must have an active outcome")
            if connection.execute("SELECT 1 FROM tasks WHERE id = ?", (safe.id,)).fetchone():
                raise ValueError(f"task already exists: {safe.id}")
            bound = safe.model_copy(update={"session_id": session_id})
            self._insert_task_row(connection, bound)
            _, _ = self._journal(
                connection,
                session.model_copy(
                    update={
                        "active_task_id": bound.id,
                        "version": session.version + 1,
                        "updated_at": datetime.now(UTC),
                    }
                ),
                event_id=journal_event_id("task.started", bound.id, bound.version),
                event_type="task.started",
                task_id=bound.id,
                data={"outcome": bound.outcome.value, "condition": bound.runtime_condition.value},
            )
            return bound

    # -- Conditional runtime store surface (SRF-02) ------------------------

    def create_task(self, task: Task) -> Task:
        """Create a Task without the legacy upsert semantics."""

        safe = self._redacted_task(task)
        with connect_write(self.path) as connection:
            if self._task_row(connection, safe.id) is not None:
                raise ValueError(f"task already exists: {safe.id}")
            if safe.session_id is not None:
                session = self._require_session(connection, safe.session_id)
            self._insert_task_row(connection, safe)
            if safe.session_id is not None:
                self._journal(
                    connection,
                    session,
                    event_id=journal_event_id("task.created", safe.id, safe.version),
                    event_type="task.created",
                    task_id=safe.id,
                )
        return safe

    def update_task(self, task: Task, *, expected_version: int) -> Task:
        """Conditionally replace a Task and fence stale writers."""

        safe = self._redacted_task(task)
        with connect_write(self.path) as connection:
            current = self._require_task(connection, safe.id)
            self._check_version(safe.id, current.version, expected_version)
            self._validate_task_update(current, safe)
            updated = safe.model_copy(
                update={"version": current.version + 1, "updated_at": datetime.now(UTC)}
            )
            self._write_task_row(connection, updated)
            session: Session | None = None
            if updated.outcome is not TaskOutcome.ACTIVE and updated.session_id is not None:
                session = self._require_session(connection, updated.session_id)
                if session.active_task_id == updated.id:
                    session = session.model_copy(
                        update={
                            "active_task_id": None,
                            "version": session.version + 1,
                            "updated_at": datetime.now(UTC),
                        }
                    )
            elif updated.session_id is not None:
                session = self._require_session(connection, updated.session_id)
            if session is not None:
                self._journal(
                    connection,
                    session,
                    event_id=journal_event_id("task.updated", updated.id, updated.version),
                    event_type="task.updated",
                    task_id=updated.id,
                    data={
                        "outcome": updated.outcome.value,
                        "condition": updated.runtime_condition.value,
                        "version": updated.version,
                    },
                )
            return updated

    def claim_execution(
        self, execution: Execution, *, expected_version: int | None = None
    ) -> Execution:
        safe = self._redacted_model(execution, Execution)
        with connect_write(self.path) as connection:
            task = self._require_task(connection, safe.task_id)
            self._check_version(task.id, task.version, expected_version)
            if task.session_id != safe.session_id:
                raise ValueError("execution session does not own its task")
            active = connection.execute(
                """
                SELECT payload_json FROM executions
                WHERE task_id = ? AND status IN (?, ?, ?, ?)
                ORDER BY generation DESC LIMIT 1
                """,
                (
                    safe.task_id,
                    ExecutionStatus.CLAIMED.value,
                    ExecutionStatus.RUNNING.value,
                    ExecutionStatus.WAITING_FOR_APPROVAL.value,
                    ExecutionStatus.PAUSED.value,
                ),
            ).fetchone()
            if active is not None:
                owner = Execution.model_validate_json(active["payload_json"]).owner_id
                raise LeaseConflict(safe.task_id, owner)
            if connection.execute("SELECT 1 FROM executions WHERE id = ?", (safe.id,)).fetchone():
                raise ValueError(f"execution already exists: {safe.id}")
            claimed = safe.model_copy(
                update={"status": ExecutionStatus.RUNNING, "updated_at": datetime.now(UTC)}
            )
            self._insert_execution_row(connection, claimed)
            running_task = task.model_copy(
                update={
                    "runtime_condition": TaskRuntimeCondition.RUNNING,
                    "version": task.version + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            self._write_task_row(connection, running_task)
            self._journal(
                connection,
                self._require_session(connection, safe.session_id),
                event_id=journal_event_id("execution.claimed", claimed.id, claimed.generation),
                event_type="execution.claimed",
                task_id=task.id,
                data={
                    "execution_id": claimed.id,
                    "owner_id": claimed.owner_id,
                    "generation": claimed.generation,
                },
            )
            return claimed

    def get_execution(self, execution_id: str) -> Execution:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM executions WHERE id = ?", (execution_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"execution not found: {execution_id}")
        return Execution.model_validate_json(row["payload_json"])

    def prepare_effects(
        self,
        effects: Sequence[Effect],
        *,
        expected_version: int,
        lease_guard: LeaseGuard | None = None,
    ) -> list[Effect]:
        if not effects:
            return []
        safe_effects = [self._redacted_effect(effect) for effect in effects]
        task_ids = {effect.task_id for effect in safe_effects}
        if len(task_ids) != 1:
            raise ValueError("one prepare batch must belong to one task")
        task_id = next(iter(task_ids))
        with connect_write(self.path) as connection:
            if lease_guard is not None:
                self._assert_guard(connection, lease_guard)
            task = self._require_task(connection, task_id)
            prepared: list[Effect] = []
            new_effects: list[Effect] = []
            for effect in safe_effects:
                existing = self._effect_by_id_or_identity(connection, effect)
                if existing is not None:
                    existing.assert_identity_compatible(effect)
                    prepared.append(existing)
                    continue
                pending = next(
                    (
                        candidate
                        for candidate in new_effects
                        if candidate.id == effect.id
                        or candidate.identity_key() == effect.identity_key()
                    ),
                    None,
                )
                if pending is not None:
                    pending.assert_identity_compatible(effect)
                    prepared.append(pending)
                    continue
                new_effects.append(effect)
                prepared.append(effect)
            if new_effects:
                self._check_version(task_id, task.version, expected_version)
                for effect in new_effects:
                    self._insert_effect_row(connection, effect)
                if task.session_id is not None:
                    effect_ids = [effect.id for effect in new_effects]
                    self._journal(
                        connection,
                        self._require_session(connection, task.session_id),
                        event_id=journal_event_id(
                            "effects.prepared", task.id, ",".join(effect_ids)
                        ),
                        event_type="effects.prepared",
                        task_id=task.id,
                        data={"effect_ids": effect_ids},
                    )
            return prepared

    def get_effect(self, effect_id: str) -> Effect:
        with connect(self.path) as connection:
            effect = self._effect_row(connection, effect_id)
        if effect is None:
            raise KeyError(f"effect not found: {effect_id}")
        return effect

    def list_effects(self, task_id: str) -> list[Effect]:
        with connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM effects
                WHERE task_id = ? ORDER BY step_id, batch_position
                """,
                (task_id,),
            ).fetchall()
        return [Effect.model_validate_json(row["payload_json"]) for row in rows]

    def decide_approval(
        self, approval: Approval, *, expected_version: int | None = None
    ) -> Approval:
        safe = self._redacted_model(approval, Approval)
        with connect_write(self.path) as connection:
            self._require_effect(connection, safe.effect_id)
            row = connection.execute(
                "SELECT payload_json FROM approvals WHERE effect_id = ?", (safe.effect_id,)
            ).fetchone()
            if row is None:
                self._insert_approval_row(connection, safe)
                self._journal_approval(connection, safe, "approval.requested")
                return safe
            existing = Approval.model_validate_json(row["payload_json"])
            if existing.id != safe.id:
                raise ApprovalConflict(safe.id, existing.status.value, safe.status.value)
            if existing == safe:
                return existing
            self._check_version(safe.id, existing.version, expected_version)
            if existing.status is not ApprovalStatus.PENDING or safe.status not in {
                ApprovalStatus.APPROVED,
                ApprovalStatus.DENIED,
                ApprovalStatus.EXPIRED,
            }:
                raise ApprovalConflict(safe.id, existing.status.value, safe.status.value)
            if safe.version != existing.version + 1:
                raise StaleVersion(safe.id, existing.version + 1, safe.version)
            connection.execute(
                """
                UPDATE approvals SET
                    status = ?, decision_source = ?, decided_at = ?,
                    version = ?, payload_json = ?
                WHERE id = ?
                """,
                (
                    safe.status.value,
                    safe.decision_source,
                    None if safe.decided_at is None else safe.decided_at.isoformat(),
                    safe.version,
                    self._redacted_json(safe),
                    safe.id,
                ),
            )
            self._journal_approval(connection, safe, "approval.decided")
            return safe

    def claim_effect(
        self, effect_id: str, *, expected_version: int, lease_guard: LeaseGuard
    ) -> Effect:
        with connect_write(self.path) as connection:
            self._assert_guard(connection, lease_guard)
            current = self._require_effect(connection, effect_id)
            self._check_version(effect_id, current.version, expected_version)
            if current.status is not EffectStatus.PREPARED:
                raise LeaseConflict(effect_id)
            claimed = current.model_copy(
                update={
                    "status": EffectStatus.EXECUTING,
                    "version": current.version + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            self._write_effect_row(connection, claimed)
            task = self._require_task(connection, claimed.task_id)
            if task.session_id is not None:
                self._journal(
                    connection,
                    self._require_session(connection, task.session_id),
                    event_id=journal_event_id("effect.claimed", claimed.id, claimed.version),
                    event_type="effect.claimed",
                    task_id=task.id,
                    trace_id=claimed.provider_call_id,
                    data={"effect_id": claimed.id},
                )
            return claimed

    def commit_effect(
        self,
        effect: Effect,
        *,
        expected_version: int,
        result_ref: str | None,
        observation_ref: str | None,
        lease_guard: LeaseGuard,
    ) -> Effect:
        """Atomically persist an Effect result, replay cursor, and journal event."""

        safe = self._redacted_model(effect, Effect)
        with connect_write(self.path) as connection:
            self._assert_guard(connection, lease_guard)
            current = self._require_effect(connection, safe.id)
            self._check_version(safe.id, current.version, expected_version)
            current.assert_identity_compatible(safe)
            if current.status is not EffectStatus.EXECUTING:
                raise ValueError(f"effect is not executing: {safe.id}")
            if safe.status not in {
                EffectStatus.SUCCEEDED,
                EffectStatus.FAILED,
                EffectStatus.UNKNOWN,
            }:
                raise ValueError(f"effect commit requires a terminal status: {safe.id}")
            committed = safe.model_copy(
                update={
                    "result_ref": result_ref,
                    "observation_ref": observation_ref,
                    "version": current.version + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            self._write_effect_row(connection, committed)
            task = self._require_task(connection, committed.task_id)
            if task.session_id is None:
                raise ValueError("effect task is not bound to a session")
            session = self._require_session(connection, task.session_id)
            sequence = session.event_sequence + 1
            event = SessionEvent(
                id=effect_commit_event_id(committed.id, committed.version),
                session_id=session.id,
                task_id=task.id,
                trace_id=committed.provider_call_id,
                sequence=sequence,
                type="effect.committed",
                data={
                    "effect_id": committed.id,
                    "status": committed.status.value,
                    "result_ref": result_ref,
                    "observation_ref": observation_ref,
                },
            )
            self._insert_event_row(connection, event)
            advanced_session = session.model_copy(
                update={"event_sequence": sequence, "updated_at": datetime.now(UTC)}
            )
            self._write_session_row(connection, advanced_session)
            self._advance_checkpoint_event_cursor(connection, task.id, sequence)
            return committed

    def append_event(
        self, event: SessionEvent, *, expected_sequence: int | None = None
    ) -> SessionEvent:
        safe = self._redacted_model(event, SessionEvent)
        with connect_write(self.path) as connection:
            session = self._require_session(connection, safe.session_id)
            existing = connection.execute(
                "SELECT payload_json FROM session_events WHERE id = ?", (safe.id,)
            ).fetchone()
            if existing is not None:
                persisted = SessionEvent.model_validate_json(existing["payload_json"])
                comparable = safe.model_copy(update={"sequence": persisted.sequence})
                if persisted.model_dump() == comparable.model_dump():
                    return persisted
                raise ValueError(f"event already exists with different content: {safe.id}")
            if expected_sequence is not None and session.event_sequence != expected_sequence:
                raise StaleVersion(safe.session_id, expected_sequence, session.event_sequence)
            assigned = safe.model_copy(update={"sequence": session.event_sequence + 1})
            self._insert_event_row(connection, assigned)
            self._write_session_row(
                connection,
                session.model_copy(
                    update={
                        "event_sequence": assigned.sequence,
                        "updated_at": datetime.now(UTC),
                    }
                ),
            )
            return assigned

    def list_events(self, session_id: str, *, after_sequence: int = 0) -> list[SessionEvent]:
        with connect(self.path) as connection:
            self._require_session(connection, session_id)
            rows = connection.execute(
                """
                SELECT payload_json FROM session_events
                WHERE session_id = ? AND sequence > ? ORDER BY sequence
                """,
                (session_id, after_sequence),
            ).fetchall()
        return [SessionEvent.model_validate_json(row["payload_json"]) for row in rows]

    def request_pause(self, request: ControlRequest, *, expected_version: int) -> ControlRequest:
        if request.kind is not ControlKind.PAUSE:
            raise ValueError("pause request must use the pause control kind")
        return self.request_control(request, expected_version=expected_version)

    def request_cancel(self, request: ControlRequest, *, expected_version: int) -> ControlRequest:
        if request.kind is not ControlKind.CANCEL:
            raise ValueError("cancel request must use the cancel control kind")
        return self.request_control(request, expected_version=expected_version)

    def request_control(self, request: ControlRequest, *, expected_version: int) -> ControlRequest:
        safe = self._redacted_model(request, ControlRequest)
        with connect_write(self.path) as connection:
            task = self._require_task(connection, safe.task_id)
            self._check_version(task.id, task.version, expected_version)
            if connection.execute(
                "SELECT 1 FROM control_requests WHERE id = ?", (safe.id,)
            ).fetchone():
                raise ValueError(f"control request already exists: {safe.id}")
            connection.execute(
                """
                INSERT INTO control_requests (
                    id, task_id, execution_id, kind, status, version,
                    requested_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    safe.id,
                    safe.task_id,
                    safe.execution_id,
                    safe.kind.value,
                    safe.status.value,
                    safe.version,
                    safe.requested_at.isoformat(),
                    self._redacted_json(safe),
                ),
            )
            if task.session_id is not None:
                self._journal(
                    connection,
                    self._require_session(connection, task.session_id),
                    event_id=journal_event_id("control.requested", safe.id, safe.version),
                    event_type="control.requested",
                    task_id=task.id,
                    data={"control_id": safe.id, "kind": safe.kind.value},
                )
            return safe

    def settle_control(
        self, request_id: str, *, status: ControlStatus, expected_version: int
    ) -> ControlRequest:
        with connect_write(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM control_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"control request not found: {request_id}")
            current = ControlRequest.model_validate_json(row["payload_json"])
            self._check_version(request_id, current.version, expected_version)
            current.transition(status)
            connection.execute(
                """
                UPDATE control_requests SET status = ?, version = ?, payload_json = ?
                WHERE id = ?
                """,
                (current.status.value, current.version, self._redacted_json(current), current.id),
            )
            task = self._require_task(connection, current.task_id)
            if task.session_id is not None:
                self._journal(
                    connection,
                    self._require_session(connection, task.session_id),
                    event_id=journal_event_id("control.updated", current.id, current.version),
                    event_type="control.updated",
                    task_id=task.id,
                    data={"control_id": current.id, "status": current.status.value},
                )
            return current

    def resolve_recovery(
        self,
        disposition: RecoveryDisposition,
        *,
        task_id: str,
        expected_version: int,
    ) -> Task:
        safe = self._redacted_model(disposition, RecoveryDisposition)
        with connect_write(self.path) as connection:
            task = self._require_task(connection, task_id)
            self._check_version(task_id, task.version, expected_version)
            if task.runtime_condition is not TaskRuntimeCondition.RECOVERY_REQUIRED:
                raise RecoveryRequired(task_id, safe.unknown_effect_id)
            effect = self._require_effect(connection, safe.unknown_effect_id)
            if effect.task_id != task_id or effect.status is not EffectStatus.UNKNOWN:
                raise RecoveryRequired(task_id, safe.unknown_effect_id)
            target = {
                RecoveryDispositionKind.CONFIRM_RESULT: TaskRuntimeCondition.RUNNING,
                RecoveryDispositionKind.CREATE_RETRY: TaskRuntimeCondition.WAITING_FOR_APPROVAL,
                RecoveryDispositionKind.ABANDON: TaskRuntimeCondition.ENDED,
            }[safe.kind]
            safe.validate_target(target)
            connection.execute(
                """
                INSERT INTO recovery_dispositions (
                    id, unknown_effect_id, kind, retry_effect_id,
                    decision_source, created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    safe.id,
                    safe.unknown_effect_id,
                    safe.kind.value,
                    safe.retry_effect_id,
                    safe.decision_source,
                    safe.created_at.isoformat(),
                    self._redacted_json(safe),
                ),
            )
            updated = task.model_copy(
                update={
                    "runtime_condition": target,
                    "version": task.version + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            self._write_task_row(connection, updated)
            if task.session_id is not None:
                self._journal(
                    connection,
                    self._require_session(connection, task.session_id),
                    event_id=journal_event_id("recovery.resolved", safe.id, safe.kind.value),
                    event_type="recovery.resolved",
                    task_id=task.id,
                    data={
                        "disposition_id": safe.id,
                        "effect_id": safe.unknown_effect_id,
                        "kind": safe.kind.value,
                    },
                )
            return updated

    def commit_checkpoint(
        self,
        checkpoint: SessionCheckpoint,
        *,
        expected_version: int,
        lease_guard: LeaseGuard,
    ) -> SessionCheckpoint:
        safe = self._redacted_model(checkpoint, SessionCheckpoint)
        with connect_write(self.path) as connection:
            self._assert_guard(connection, lease_guard)
            task = self._require_task(connection, safe.task_id)
            self._check_version(task.id, task.version, expected_version)
            if task.session_id != safe.session_id:
                raise ValueError("checkpoint session does not own its task")
            session = self._require_session(connection, safe.session_id)
            self._validate_checkpoint_references(connection, safe, session)
            connection.execute(
                """
                INSERT INTO checkpoints (task_id, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (safe.task_id, self._redacted_json(safe), safe.updated_at.isoformat()),
            )
            self._journal(
                connection,
                session,
                event_id=journal_event_id(
                    "checkpoint.committed", safe.task_id, safe.updated_at.isoformat()
                ),
                event_type="checkpoint.committed",
                task_id=safe.task_id,
                data={"consumed_input_sequence": safe.consumed_input_sequence},
            )
            return safe

    def get_session_checkpoint(self, task_id: str) -> SessionCheckpoint:
        with connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM checkpoints WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"checkpoint:{task_id}")
        return _adapt_checkpoint_json(row["payload_json"], SessionCheckpoint)

    def commit_tool_result(
        self,
        task_id: str,
        call: ToolCall,
        result: ToolResult,
        *,
        expected_version: int | None = None,
        lease_guard: LeaseGuard | None = None,
    ) -> ToolResult:
        """Persist a replayable legacy tool observation with conflict checks."""

        safe_call = self._redacted_model(call, ToolCall)
        safe_result = self._redacted_model(result, ToolResult)
        if safe_result.call_id != safe_call.id:
            raise ValueError("tool result does not belong to the call")
        with connect_write(self.path) as connection:
            if lease_guard is not None:
                self._assert_guard(connection, lease_guard)
            task = self._require_task(connection, task_id)
            row = connection.execute(
                """
                SELECT call_json, result_json FROM tool_calls
                WHERE task_id = ? AND call_id = ?
                """,
                (task_id, safe_call.id),
            ).fetchone()
            if row is not None:
                persisted_call = ToolCall.model_validate_json(row["call_json"])
                persisted_result = ToolResult.model_validate_json(row["result_json"])
                if persisted_call != safe_call or persisted_result != safe_result:
                    raise ValueError(
                        f"tool call already committed with different content: {safe_call.id}"
                    )
                return persisted_result
            self._check_version(task_id, task.version, expected_version)
            connection.execute(
                """
                INSERT INTO tool_calls
                    (call_id, task_id, call_json, result_json, completed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    safe_call.id,
                    task_id,
                    self._redacted_json(safe_call),
                    self._redacted_json(safe_result),
                    datetime.now(UTC).isoformat(),
                ),
            )
            if task.session_id is not None:
                self._journal(
                    connection,
                    self._require_session(connection, task.session_id),
                    event_id=journal_event_id("tool_result.committed", safe_call.id, task_id),
                    event_type="tool_result.committed",
                    task_id=task_id,
                    trace_id=safe_call.id,
                    data={
                        "call_id": safe_call.id,
                        "tool_name": safe_call.name,
                        "success": safe_result.success,
                    },
                )
            return safe_result

    @staticmethod
    def _check_version(entity_id: str, actual: int, expected: int | None) -> None:
        if expected is not None and actual != expected:
            raise StaleVersion(entity_id, expected, actual)

    @staticmethod
    def _validate_task_update(current: Task, requested: Task) -> None:
        if current.session_id != requested.session_id:
            raise ValueError("task session binding is immutable")
        if current.outcome is not TaskOutcome.ACTIVE and requested.outcome is not current.outcome:
            raise ValueError("terminal task outcome is immutable")
        if (
            current.runtime_condition is TaskRuntimeCondition.RECOVERY_REQUIRED
            and requested.runtime_condition is not TaskRuntimeCondition.RECOVERY_REQUIRED
        ):
            raise RecoveryRequired(current.id)

    @staticmethod
    def _validate_checkpoint_references(
        connection: sqlite3.Connection,
        checkpoint: SessionCheckpoint,
        session: Session,
    ) -> None:
        if checkpoint.event_sequence > session.event_sequence:
            raise ValueError("checkpoint event cursor is ahead of the session journal")
        max_turn_sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM turns WHERE session_id = ?",
                (session.id,),
            ).fetchone()[0]
        )
        if checkpoint.consumed_input_sequence > max_turn_sequence:
            raise ValueError("checkpoint input cursor is ahead of persisted turns")
        if checkpoint.turn_id is not None:
            turn = connection.execute(
                "SELECT session_id, task_id FROM turns WHERE id = ?", (checkpoint.turn_id,)
            ).fetchone()
            if (
                turn is None
                or turn["session_id"] != session.id
                or turn["task_id"] not in {None, checkpoint.task_id}
            ):
                raise ValueError("checkpoint turn is not part of its session/task")
        if len(checkpoint.pending_effect_ids) != len(set(checkpoint.pending_effect_ids)):
            raise ValueError("checkpoint pending Effect IDs must be unique")
        for effect_id in checkpoint.pending_effect_ids:
            row = connection.execute(
                "SELECT task_id FROM effects WHERE id = ?", (effect_id,)
            ).fetchone()
            if row is None or row["task_id"] != checkpoint.task_id:
                raise ValueError("checkpoint references an Effect outside its task")

    def _redacted_model[ModelT: BaseModel](
        self, model: BaseModel, model_type: type[ModelT]
    ) -> ModelT:
        return model_type.model_validate(self.redactor.redact(model.model_dump(mode="json")))

    def _redacted_session(self, session: Session) -> Session:
        return Session.model_validate(self.redactor.redact(session.model_dump(mode="json")))

    def _redacted_turn(self, turn: Turn) -> Turn:
        return Turn.model_validate(self.redactor.redact(turn.model_dump(mode="json")))

    @staticmethod
    def _same_turn_submission(existing: Turn, requested: Turn) -> bool:
        return (
            existing.role is requested.role
            and existing.content == requested.content
            and existing.resource_refs == requested.resource_refs
            and (requested.task_id is None or existing.task_id == requested.task_id)
        )

    def _redacted_task(self, task: Task) -> Task:
        return Task.model_validate(self.redactor.redact(task.model_dump(mode="json")))

    def _redacted_effect(self, effect: Effect) -> Effect:
        persisted = persist_tool_arguments(
            effect.arguments_summary,
            credential_bindings=effect.credential_bindings,
            redactor=self.redactor,
        )
        normalized = effect.model_copy(
            update={
                "arguments_summary": persisted.arguments,
                "credential_bindings": persisted.credential_bindings,
                "redacted_argument_paths": persisted.redacted_paths,
            }
        )
        return self._redacted_model(normalized, Effect)

    @staticmethod
    def _task_row(connection: sqlite3.Connection, task_id: str) -> Task | None:
        row = connection.execute(
            "SELECT payload_json FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return None if row is None else Task.model_validate_json(row["payload_json"])

    def _require_task(self, connection: sqlite3.Connection, task_id: str) -> Task:
        task = self._task_row(connection, task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    @staticmethod
    def _session_row(connection: sqlite3.Connection, session_id: str) -> Session | None:
        row = connection.execute(
            "SELECT payload_json FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return None if row is None else Session.model_validate_json(row["payload_json"])

    def _require_session(self, connection: sqlite3.Connection, session_id: str) -> Session:
        session = self._session_row(connection, session_id)
        if session is None:
            raise KeyError(f"session not found: {session_id}")
        return session

    @staticmethod
    def _turn_by_submission(
        connection: sqlite3.Connection, session_id: str, client_submission_id: str
    ) -> Turn | None:
        row = connection.execute(
            """
            SELECT payload_json FROM turns
            WHERE session_id = ? AND client_submission_id = ?
            """,
            (session_id, client_submission_id),
        ).fetchone()
        return None if row is None else Turn.model_validate_json(row["payload_json"])

    def _insert_session_row(self, connection: sqlite3.Connection, session: Session) -> None:
        connection.execute(
            """
            INSERT INTO sessions (
                id, workspace_ref, status, active_task_id, config_version,
                version, event_sequence, created_at, updated_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.workspace_ref,
                session.status.value,
                session.active_task_id,
                session.config_version,
                session.version,
                session.event_sequence,
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
                self._redacted_json(session),
            ),
        )

    def _write_session_row(self, connection: sqlite3.Connection, session: Session) -> None:
        connection.execute(
            """
            UPDATE sessions SET
                workspace_ref = ?, status = ?, active_task_id = ?, config_version = ?,
                version = ?, event_sequence = ?, created_at = ?, updated_at = ?,
                payload_json = ?
            WHERE id = ?
            """,
            (
                session.workspace_ref,
                session.status.value,
                session.active_task_id,
                session.config_version,
                session.version,
                session.event_sequence,
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
                self._redacted_json(session),
                session.id,
            ),
        )

    def _insert_task_row(self, connection: sqlite3.Connection, task: Task) -> None:
        connection.execute(
            """
            INSERT INTO tasks (
                id, status, payload_json, updated_at,
                session_id, outcome, runtime_condition, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.id,
                task.status,
                self._redacted_json(task),
                task.updated_at.isoformat(),
                task.session_id,
                task.outcome.value,
                task.runtime_condition.value,
                task.version,
            ),
        )

    def _write_task_row(self, connection: sqlite3.Connection, task: Task) -> None:
        cursor = connection.execute(
            """
            UPDATE tasks SET
                status = ?, payload_json = ?, updated_at = ?, session_id = ?,
                outcome = ?, runtime_condition = ?, version = ?
            WHERE id = ?
            """,
            (
                task.status,
                self._redacted_json(task),
                task.updated_at.isoformat(),
                task.session_id,
                task.outcome.value,
                task.runtime_condition.value,
                task.version,
                task.id,
            ),
        )
        if cursor.rowcount != 1:
            raise TaskNotFoundError(task.id)

    def _insert_execution_row(self, connection: sqlite3.Connection, execution: Execution) -> None:
        connection.execute(
            """
            INSERT INTO executions (
                id, session_id, task_id, owner_id, lease_token, generation,
                lease_expires_at, status, version, created_at, updated_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution.id,
                execution.session_id,
                execution.task_id,
                execution.owner_id,
                execution.lease_token,
                execution.generation,
                execution.lease_expires_at.isoformat(),
                execution.status.value,
                execution.version,
                execution.created_at.isoformat(),
                execution.updated_at.isoformat(),
                self._redacted_json(execution),
            ),
        )

    def _assert_guard(self, connection: sqlite3.Connection, guard: LeaseGuard) -> Execution:
        row = connection.execute(
            "SELECT payload_json FROM executions WHERE id = ?", (guard.execution_id,)
        ).fetchone()
        if row is None:
            raise LeaseLost(guard.task_id)
        execution = Execution.model_validate_json(row["payload_json"])
        if (
            execution.task_id != guard.task_id
            or execution.lease_token != guard.token
            or execution.generation != guard.generation
            or execution.status
            not in {
                ExecutionStatus.CLAIMED,
                ExecutionStatus.RUNNING,
                ExecutionStatus.WAITING_FOR_APPROVAL,
                ExecutionStatus.PAUSED,
            }
        ):
            raise LeaseLost(guard.task_id)
        return execution

    @staticmethod
    def _effect_row(connection: sqlite3.Connection, effect_id: str) -> Effect | None:
        row = connection.execute(
            "SELECT payload_json FROM effects WHERE id = ?", (effect_id,)
        ).fetchone()
        return None if row is None else Effect.model_validate_json(row["payload_json"])

    def _require_effect(self, connection: sqlite3.Connection, effect_id: str) -> Effect:
        effect = self._effect_row(connection, effect_id)
        if effect is None:
            raise KeyError(f"effect not found: {effect_id}")
        return effect

    def _effect_by_id_or_identity(
        self, connection: sqlite3.Connection, effect: Effect
    ) -> Effect | None:
        row = connection.execute(
            """
            SELECT payload_json FROM effects
            WHERE id = ? OR (task_id = ? AND step_id = ? AND batch_position = ?)
            LIMIT 1
            """,
            (effect.id, effect.task_id, effect.step_id, effect.batch_position),
        ).fetchone()
        return None if row is None else Effect.model_validate_json(row["payload_json"])

    def _insert_effect_row(self, connection: sqlite3.Connection, effect: Effect) -> None:
        connection.execute(
            """
            INSERT INTO effects (
                id, task_id, step_id, batch_position, provider_call_id,
                retry_of_effect_id, status, approval_id, version,
                created_at, updated_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                effect.id,
                effect.task_id,
                effect.step_id,
                effect.batch_position,
                effect.provider_call_id,
                effect.retry_of_effect_id,
                effect.status.value,
                effect.approval_id,
                effect.version,
                effect.created_at.isoformat(),
                effect.updated_at.isoformat(),
                self._redacted_json(effect),
            ),
        )

    def _write_effect_row(self, connection: sqlite3.Connection, effect: Effect) -> None:
        cursor = connection.execute(
            """
            UPDATE effects SET
                status = ?, approval_id = ?, version = ?, updated_at = ?, payload_json = ?
            WHERE id = ?
            """,
            (
                effect.status.value,
                effect.approval_id,
                effect.version,
                effect.updated_at.isoformat(),
                self._redacted_json(effect),
                effect.id,
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"effect not found: {effect.id}")

    def _insert_approval_row(self, connection: sqlite3.Connection, approval: Approval) -> None:
        connection.execute(
            """
            INSERT INTO approvals (
                id, effect_id, status, decision_source, decided_at,
                version, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval.id,
                approval.effect_id,
                approval.status.value,
                approval.decision_source,
                None if approval.decided_at is None else approval.decided_at.isoformat(),
                approval.version,
                approval.created_at.isoformat(),
                self._redacted_json(approval),
            ),
        )

    def _insert_event_row(self, connection: sqlite3.Connection, event: SessionEvent) -> None:
        connection.execute(
            """
            INSERT INTO session_events (
                id, session_id, sequence, type, task_id, trace_id,
                created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.session_id,
                event.sequence,
                event.type,
                event.task_id,
                event.trace_id,
                event.timestamp.isoformat(),
                self._redacted_json(event),
            ),
        )

    def _journal(
        self,
        connection: sqlite3.Connection,
        session: Session,
        *,
        event_id: str,
        event_type: str,
        task_id: str | None = None,
        trace_id: str | None = None,
        data: dict[str, object] | None = None,
    ) -> tuple[SessionEvent, Session]:
        event = SessionEvent(
            id=event_id,
            session_id=session.id,
            type=event_type,
            task_id=task_id,
            trace_id=trace_id,
            sequence=session.event_sequence + 1,
            data={} if data is None else data,
        )
        self._insert_event_row(connection, event)
        updated = session.model_copy(
            update={"event_sequence": event.sequence, "updated_at": datetime.now(UTC)}
        )
        self._write_session_row(connection, updated)
        return event, updated

    def _journal_approval(
        self, connection: sqlite3.Connection, approval: Approval, event_type: str
    ) -> None:
        effect = self._require_effect(connection, approval.effect_id)
        task = self._require_task(connection, effect.task_id)
        if task.session_id is None:
            return
        self._journal(
            connection,
            self._require_session(connection, task.session_id),
            event_id=journal_event_id(event_type, approval.id, approval.version),
            event_type=event_type,
            task_id=task.id,
            data={
                "approval_id": approval.id,
                "effect_id": effect.id,
                "status": approval.status.value,
            },
        )

    def _advance_checkpoint_event_cursor(
        self, connection: sqlite3.Connection, task_id: str, sequence: int
    ) -> None:
        row = connection.execute(
            "SELECT payload_json FROM checkpoints WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return
        payload = json.loads(row["payload_json"])
        payload["event_sequence"] = sequence
        payload["updated_at"] = datetime.now(UTC).isoformat()
        connection.execute(
            """
            UPDATE checkpoints SET payload_json = ?, updated_at = ? WHERE task_id = ?
            """,
            (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                payload["updated_at"],
                task_id,
            ),
        )
