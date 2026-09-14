"""Public prompt-cache contracts.

The package groups provider prompt-cache diagnostics, layout assembly, cache
epochs, and memory publication.  Implementations remain pure and do not own
provider I/O, persistence, tracing, or runtime orchestration.
"""

from patchloop.prompt_cache.coordinator import (
    AppendOnlyPromptState,
    PrefixBudget,
    PromptCacheCheckpointFields,
    PromptCacheCompressionPreparation,
    PromptCacheCoordinator,
    PromptCacheCoordinatorError,
    PromptCacheCoordinatorSnapshot,
    PromptCachePreparedRequest,
    PromptCacheResponseObservation,
    PromptPrefixViolation,
    compute_prefix_budget,
)
from patchloop.prompt_cache.diagnostics import (
    CacheDiagnostics,
    CacheDiagnosticsSnapshot,
    CacheLayoutReason,
    CacheLayoutTrace,
    CacheRequestFingerprint,
    CacheSectionFingerprint,
    PromptCacheDiagnostics,
    PromptCacheDiagnosticsSnapshot,
    fingerprint_json,
    fingerprint_request,
    fingerprint_text,
)
from patchloop.prompt_cache.epoch import (
    COMPRESSION_INSTRUCTION,
    SUMMARY_PREFIX,
    CacheCompressionRequest,
    CacheEpoch,
    CacheEpochBoundary,
    CacheEpochSnapshot,
)
from patchloop.prompt_cache.layout import (
    APPEND_ONLY_MEMORY_PROTOCOL,
    PROJECT_INSTRUCTIONS_PREFIX,
    PromptLayout,
)
from patchloop.prompt_cache.publication import (
    MEMORY_DELTA_PREFIX,
    MEMORY_DELTA_V2_PREFIX,
    MEMORY_SNAPSHOT_PREFIX,
    MEMORY_SNAPSHOT_V2_PREFIX,
    MemoryDeltaPublisher,
    MemoryDeltaTooLarge,
    MemoryPublicationSnapshot,
    MemoryPublicationUpdate,
)
from patchloop.prompt_cache.usage import CacheUsageAccumulator, CacheUsageAccumulatorSnapshot

__all__ = [
    "APPEND_ONLY_MEMORY_PROTOCOL",
    "COMPRESSION_INSTRUCTION",
    "MEMORY_DELTA_PREFIX",
    "MEMORY_DELTA_V2_PREFIX",
    "MEMORY_SNAPSHOT_PREFIX",
    "MEMORY_SNAPSHOT_V2_PREFIX",
    "PROJECT_INSTRUCTIONS_PREFIX",
    "SUMMARY_PREFIX",
    "AppendOnlyPromptState",
    "CacheCompressionRequest",
    "CacheDiagnostics",
    "CacheDiagnosticsSnapshot",
    "CacheEpoch",
    "CacheEpochBoundary",
    "CacheEpochSnapshot",
    "CacheLayoutReason",
    "CacheLayoutTrace",
    "CacheRequestFingerprint",
    "CacheSectionFingerprint",
    "CacheUsageAccumulator",
    "CacheUsageAccumulatorSnapshot",
    "MemoryDeltaPublisher",
    "MemoryDeltaTooLarge",
    "MemoryPublicationSnapshot",
    "MemoryPublicationUpdate",
    "PrefixBudget",
    "PromptCacheCheckpointFields",
    "PromptCacheCompressionPreparation",
    "PromptCacheCoordinator",
    "PromptCacheCoordinatorError",
    "PromptCacheCoordinatorSnapshot",
    "PromptCacheDiagnostics",
    "PromptCacheDiagnosticsSnapshot",
    "PromptCachePreparedRequest",
    "PromptCacheResponseObservation",
    "PromptLayout",
    "PromptPrefixViolation",
    "compute_prefix_budget",
    "fingerprint_json",
    "fingerprint_request",
    "fingerprint_text",
]
