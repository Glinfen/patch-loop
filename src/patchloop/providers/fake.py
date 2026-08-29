"""Deterministic provider used by tests and local protocol demos."""

from collections.abc import Iterable

from patchloop.providers.base import ModelMessage, ModelResponse, ToolSpec


class FakeProvider:
    def __init__(self, responses: Iterable[ModelResponse]) -> None:
        self._responses = iter(responses)
        self.requests: list[tuple[list[ModelMessage], list[ToolSpec]]] = []

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
            return next(self._responses)
        except StopIteration as exc:
            raise RuntimeError("FakeProvider has no response left") from exc
