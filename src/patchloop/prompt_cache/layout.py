"""Shared prompt assembly primitives for legacy and stable cache layouts."""

from __future__ import annotations

from collections.abc import Sequence

from patchloop.domain import PromptCacheLayout
from patchloop.providers.base import ModelMessage, ToolSpec

PROJECT_INSTRUCTIONS_PREFIX = (
    "PATCHLOOP_PROJECT_INSTRUCTIONS_V1\n"
    "Trusted task-scoped project instruction snapshot; it is fixed for this cache epoch.\n"
)


class PromptLayout:
    """Assemble the stable prefix and append dynamic memory without rewriting it."""

    def __init__(self, mode: PromptCacheLayout) -> None:
        self.mode = mode

    @property
    def stable(self) -> bool:
        return self.mode is PromptCacheLayout.STABLE

    def initial_messages(
        self,
        system_prompt: str,
        goal: str,
        *,
        project_instructions: str = "",
    ) -> list[ModelMessage]:
        messages = [ModelMessage(role="system", content=system_prompt)]
        if self.stable and project_instructions.strip():
            messages.append(
                ModelMessage(
                    role="system",
                    content=PROJECT_INSTRUCTIONS_PREFIX + project_instructions,
                )
            )
        messages.append(ModelMessage(role="user", content=goal))
        return messages

    @property
    def prefix_message_count(self) -> int:
        return 3 if self.stable else 2

    @staticmethod
    def runtime_memory_message(rendered: str) -> ModelMessage | None:
        if not rendered:
            return None
        return ModelMessage(role="system", content=rendered)

    @staticmethod
    def freeze_tools(specifications: Sequence[ToolSpec]) -> list[ToolSpec]:
        """Copy the provider contract once so later registry changes cannot reorder it."""

        return [item.model_copy(deep=True) for item in specifications]
