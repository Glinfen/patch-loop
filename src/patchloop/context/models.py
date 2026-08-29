"""Structured context-window, memory, and debug models."""

from __future__ import annotations

from pydantic import BaseModel, Field

from patchloop.providers.base import ModelMessage


class MemoryEvidence(BaseModel):
    step_index: int = Field(ge=0)
    source: str
    success: bool
    summary: str
    paths: list[str] = Field(default_factory=list)


class TaskMemory(BaseModel):
    omitted_step_count: int = Field(default=0, ge=0)
    omitted_step_indices: list[int] = Field(default_factory=list)
    unfinished_items: list[str] = Field(default_factory=list)
    key_evidence: list[MemoryEvidence] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)


class ContextSelection(BaseModel):
    step_index: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    relevance: float = Field(ge=0, le=1)
    reason: str


class ContextDebug(BaseModel):
    budget_tokens: int = Field(ge=1)
    estimated_tokens: int = Field(ge=0)
    message_tokens: int = Field(ge=0)
    tool_spec_tokens: int = Field(ge=0)
    original_message_tokens: int = Field(ge=0)
    selected_steps: list[ContextSelection] = Field(default_factory=list)
    dropped_steps: list[int] = Field(default_factory=list)
    memory_budget_tokens: int = Field(ge=0)
    memory_tokens: int = Field(ge=0)
    truncated_messages: int = Field(ge=0)

    def render(self, width: int = 24) -> str:
        used = min(self.estimated_tokens, self.budget_tokens)
        filled = round(width * used / self.budget_tokens)
        bar = "#" * filled + "-" * (width - filled)
        return (
            f"context [{bar}] {self.estimated_tokens}/{self.budget_tokens} tokens | "
            f"selected={len(self.selected_steps)} dropped={len(self.dropped_steps)} "
            f"memory={self.memory_tokens}"
        )


class ContextWindow(BaseModel):
    messages: list[ModelMessage]
    memory: TaskMemory | None = None
    debug: ContextDebug
