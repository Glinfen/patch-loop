"""SQLite persistence for tasks, steps, tool calls, checkpoints, and artifacts."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from patchloop.domain import AgentStep, Plan, Task, TaskStatus, ToolCall, ToolResult
from patchloop.providers.base import ModelMessage
from patchloop.storage import TaskNotFoundError


class RuntimeCheckpoint(BaseModel):
    task_id: str
    next_step_index: int = Field(ge=0)
    messages: list[ModelMessage]
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
    elapsed_seconds: float = Field(default=0.0, ge=0)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_steps (
                    task_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (task_id, step_index),
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS tool_calls (
                    task_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    call_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, call_id),
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    task_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    task_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, name),
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                """
            )

    def save_task(self, task: Task) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (id, status, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    task.id,
                    task.status,
                    task.model_dump_json(),
                    task.updated_at.isoformat(),
                ),
            )

    def get_task(self, task_id: str) -> Task:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFoundError(task_id)
        return Task.model_validate_json(row["payload_json"])

    def record_step(self, step: AgentStep) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_steps (task_id, step_index, payload_json)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id, step_index) DO UPDATE SET
                    payload_json = excluded.payload_json
                """,
                (step.task_id, step.index, step.model_dump_json()),
            )

    def list_steps(self, task_id: str) -> list[AgentStep]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM agent_steps
                WHERE task_id = ? ORDER BY step_index
                """,
                (task_id,),
            ).fetchall()
        return [AgentStep.model_validate_json(row["payload_json"]) for row in rows]

    def record_tool_call(self, task_id: str, call: ToolCall, result: ToolResult) -> None:
        with self._connect() as connection:
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
                    call.model_dump_json(),
                    result.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get_tool_result(self, task_id: str, call_id: str) -> ToolResult | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT result_json FROM tool_calls
                WHERE task_id = ? AND call_id = ?
                """,
                (task_id, call_id),
            ).fetchone()
        return None if row is None else ToolResult.model_validate_json(row["result_json"])

    def list_tool_results(self, task_id: str) -> list[ToolResult]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT result_json FROM tool_calls
                WHERE task_id = ? ORDER BY completed_at, call_id
                """,
                (task_id,),
            ).fetchall()
        return [ToolResult.model_validate_json(row["result_json"]) for row in rows]

    def save_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        with self._connect() as connection:
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
                    checkpoint.model_dump_json(),
                    checkpoint.updated_at.isoformat(),
                ),
            )

    def get_checkpoint(self, task_id: str) -> RuntimeCheckpoint:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM checkpoints WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"checkpoint:{task_id}")
        return RuntimeCheckpoint.model_validate_json(row["payload_json"])

    def record_artifact(self, task_id: str, path: Path) -> None:
        with self._connect() as connection:
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
        with self._connect() as connection:
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
