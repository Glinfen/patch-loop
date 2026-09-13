"""Serializable models for long-lived user sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.domain import Plan, SessionStatus, ToolResult
from patchloop.memory.episodic import EpisodicMemorySnapshot
from patchloop.memory.manager import MemoryManagerSnapshot
from patchloop.memory.working import WorkingMemorySnapshot
from patchloop.prompt_cache import (
    AppendOnlyPromptState,
    CacheEpochSnapshot,
    MemoryPublicationSnapshot,
)
from patchloop.providers.base import ModelMessage, ToolSpec

SESSION_SCHEMA_VERSION: Literal["1.0"] = "1.0"


def _now() -> datetime:
    return datetime.now(UTC)


class TurnRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class Session(BaseModel):
    """A long-lived conversation bound to one workspace."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal["1.0"] = SESSION_SCHEMA_VERSION
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    workspace_ref: str = Field(min_length=1)
    status: SessionStatus = SessionStatus.OPEN
    active_task_id: str | None = None
    config_version: str = Field(default="1", min_length=1, max_length=128)
    version: int = Field(default=1, ge=1)
    event_sequence: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    def close(self) -> None:
        if self.status is SessionStatus.CLOSED:
            return
        if self.active_task_id is not None:
            raise ValueError("cannot close a session with an active task")
        object.__setattr__(self, "status", SessionStatus.CLOSED)
        object.__setattr__(self, "version", self.version + 1)
        object.__setattr__(self, "updated_at", _now())


class Turn(BaseModel):
    """One persisted user/agent interaction in a session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = SESSION_SCHEMA_VERSION
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    session_id: str = Field(min_length=1)
    task_id: str | None = Field(default=None, min_length=1)
    role: TurnRole
    content: str = Field(min_length=1)
    resource_refs: list[str] = Field(default_factory=list)
    sequence: int = Field(default=0, ge=0)
    client_submission_id: str | None = Field(default=None, min_length=1, max_length=256)
    created_at: datetime = Field(default_factory=_now)


class SessionCheckpoint(BaseModel):
    """Session-aware checkpoint that keeps the legacy runtime projections."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = SESSION_SCHEMA_VERSION
    session_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    turn_id: str | None = Field(default=None, min_length=1)
    consumed_input_sequence: int = Field(default=0, ge=0)
    event_sequence: int = Field(default=0, ge=0)
    pending_effect_ids: list[str] = Field(default_factory=list)
    messages: list[ModelMessage] = Field(default_factory=list)
    tool_specifications: list[ToolSpec] | None = None
    plan: Plan | None = None
    tool_history: list[ToolResult] = Field(default_factory=list)
    working_memory: WorkingMemorySnapshot | None = None
    episodic_memory: EpisodicMemorySnapshot | None = None
    memory_manager: MemoryManagerSnapshot | None = None
    cache_epoch_state: CacheEpochSnapshot | None = None
    memory_publication_state: MemoryPublicationSnapshot | None = None
    append_only_state: AppendOnlyPromptState | None = None
    updated_at: datetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def validate_append_only_transcript(self) -> Self:
        if self.append_only_state is not None:
            self.append_only_state.validate_message_boundaries(len(self.messages))
        return self


Checkpoint = SessionCheckpoint

__all__ = [
    "SESSION_SCHEMA_VERSION",
    "Checkpoint",
    "Session",
    "SessionCheckpoint",
    "SessionStatus",
    "Turn",
    "TurnRole",
]
