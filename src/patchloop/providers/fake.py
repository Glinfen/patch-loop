"""Deterministic providers used by tests."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from patchloop.providers.base import ModelMessage, ModelResponse, ModelUsage, ToolSpec


class CacheSimulator(Protocol):
    def observe(self, *args: Any, **kwargs: Any) -> tuple[object, ModelUsage]: ...


class FakeProvider:
    def __init__(
        self,
        responses: Iterable[ModelResponse],
        *,
        cache_simulator: CacheSimulator | None = None,
    ) -> None:
        self._responses = iter(responses)
        self.requests: list[tuple[list[ModelMessage], list[ToolSpec]]] = []
        self.cache_simulator = cache_simulator

    @property
    def name(self) -> str:
        return "fake"

    def complete(
        self,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        self.requests.append((list(messages), list(tools)))
        try:
            response = next(self._responses)
        except StopIteration as exc:
            raise RuntimeError("FakeProvider has no response left") from exc
        if self.cache_simulator is None:
            return response
        _, usage = self.cache_simulator.observe(
            len(self.requests) - 1,
            messages,
            tools,
        )
        return response.model_copy(update={"usage": usage})
