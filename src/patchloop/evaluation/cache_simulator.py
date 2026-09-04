"""Deterministic prefix-cache simulation for evaluation only."""

from __future__ import annotations

from patchloop.prompt_cache import CacheDiagnostics, CacheDiagnosticsSnapshot, CacheLayoutTrace
from patchloop.providers.base import ModelMessage, ModelUsage, ToolSpec


class DeterministicPrefixCacheSimulator:
    """Simulate prefix-cache usage without retaining raw prompt data."""

    def __init__(self, *, miss_threshold_tokens: int = 70_000) -> None:
        self.diagnostics = CacheDiagnostics(miss_threshold_tokens=miss_threshold_tokens)
        self._previous_wire: bytes | None = None

    def reset(self) -> None:
        self.diagnostics.reset()
        self._previous_wire = None

    def snapshot(self) -> CacheDiagnosticsSnapshot:
        return self.diagnostics.snapshot()

    def restore(self, snapshot: CacheDiagnosticsSnapshot | None) -> None:
        self.diagnostics.restore(snapshot)
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


__all__ = ["DeterministicPrefixCacheSimulator"]
