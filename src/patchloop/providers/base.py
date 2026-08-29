"""Provider-neutral model protocol."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from patchloop.domain import ToolCall


class ModelMessage(BaseModel):
    role: str
    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]
    permission: str = "read"


class ModelUsage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)


class ModelResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)


class ModelProvider(Protocol):
    @property
    def name(self) -> str: ...

    def complete(
        self,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
    ) -> ModelResponse: ...
