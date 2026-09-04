"""Deterministic providers and prompt-cache simulation used by tests."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from patchloop.providers.base import ModelMessage, ModelResponse, ModelUsage, ToolSpec

if TYPE_CHECKING:
    from patchloop.prompt_cache import CacheDiagnosticsSnapshot, CacheLayoutTrace


class FakeProvider:
    def __init__(
        self,
        responses: Iterable[ModelResponse],
        *,
        cache_simulator: DeterministicPrefixCacheSimulator | None = None,
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


class DeterministicPrefixCacheSimulator:
    """Simulate prefix-cache usage without retaining raw prompt data.

    The simulator uses the same canonical request bytes as cache diagnostics.
    It reports a synthetic hit for the exact byte-prefix shared with the
    previous request and a miss for the remainder.  The bytes are held only in
    memory for the current run and are never part of the returned trace.
    """

    def __init__(self, *, miss_threshold_tokens: int = 70_000) -> None:
        from patchloop.prompt_cache import CacheDiagnostics

        self.diagnostics = CacheDiagnostics(miss_threshold_tokens=miss_threshold_tokens)
        self._previous_wire: bytes | None = None

    def reset(self) -> None:
        self.diagnostics.reset()
        self._previous_wire = None

    def snapshot(self) -> CacheDiagnosticsSnapshot:
        return self.diagnostics.snapshot()

    def restore(self, snapshot: CacheDiagnosticsSnapshot | None) -> None:
        self.diagnostics.restore(snapshot)
        # Checkpoint state deliberately contains no prompt bytes.  The next
        # request remains diagnosable, but the exact byte LCP is only available
        # when both requests are observed in this process.
        self._previous_wire = None

    def observe(
        self,
        step: int,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
        *,
        provider: str = "fake",
        model: str = "fake-model",
        thinking: object | None = None,
        epoch_snapshot: object | None = None,
        system_instructions: str | None = None,
        task_project_snapshot: object | None = None,
        memory_projection: object | None = None,
    ) -> tuple[CacheLayoutTrace, ModelUsage]:
        from patchloop.prompt_cache import fingerprint_request

        _, wire = fingerprint_request(
            messages,
            tools,
            model=model,
            thinking=thinking,
            epoch_snapshot=epoch_snapshot,
            system_instructions=system_instructions,
            task_project_snapshot=task_project_snapshot,
            memory_projection=memory_projection,
        )
        trace = self.diagnostics.observe(
            step,
            messages,
            tools,
            provider=provider,
            model=model,
            thinking=thinking,
            epoch_snapshot=epoch_snapshot,
            system_instructions=system_instructions,
            task_project_snapshot=task_project_snapshot,
            memory_projection=memory_projection,
        )
        lcp_bytes = _longest_common_prefix(self._previous_wire, wire)
        input_tokens = _estimate_tokens(len(wire))
        hit_tokens = min(input_tokens, _estimate_tokens(lcp_bytes))
        usage = ModelUsage(
            input_tokens=input_tokens,
            cache_hit_tokens=hit_tokens,
            cache_miss_tokens=input_tokens - hit_tokens,
        )
        trace = trace.model_copy(
            update={
                "longest_common_prefix_bytes": lcp_bytes,
                "longest_common_prefix_tokens": hit_tokens,
            }
        )
        self._previous_wire = wire
        return self.diagnostics.finalize(trace, usage), usage


def _estimate_tokens(byte_length: int) -> int:
    return 0 if byte_length <= 0 else (byte_length + 2) // 3


def _longest_common_prefix(first: bytes | None, second: bytes) -> int:
    if first is None:
        return 0
    limit = min(len(first), len(second))
    index = 0
    while index < limit and first[index] == second[index]:
        index += 1
    return index
