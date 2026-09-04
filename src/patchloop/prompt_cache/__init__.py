"""Public prompt-cache contracts.

The package groups provider prompt-cache diagnostics, layout assembly, cache
epochs, and memory publication.  Implementations remain pure and do not own
provider I/O, persistence, tracing, or runtime orchestration.
"""

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
from patchloop.prompt_cache.layout import PROJECT_INSTRUCTIONS_PREFIX, PromptLayout
from patchloop.prompt_cache.publication import (
    MEMORY_DELTA_PREFIX,
    MEMORY_SNAPSHOT_PREFIX,
    MemoryDeltaPublisher,
    MemoryDeltaTooLarge,
    MemoryPublicationSnapshot,
)
from patchloop.prompt_cache.usage import CacheUsageAccumulator, CacheUsageAccumulatorSnapshot
from patchloop.prompt_cache.coordinator import (
    PromptCacheCompressionPreparation,
    PromptCacheCoordinator,
    PromptCacheCoordinatorError,
    PromptCacheCoordinatorSnapshot,
    PromptCachePreparedRequest,
    PromptCacheResponseObservation,
)

__all__ = [
    "COMPRESSION_INSTRUCTION",
    "MEMORY_DELTA_PREFIX",
    "MEMORY_SNAPSHOT_PREFIX",
    "PROJECT_INSTRUCTIONS_PREFIX",
    "SUMMARY_PREFIX",
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
    "PromptCacheCompressionPreparation",
    "PromptCacheCoordinator",
    "PromptCacheCoordinatorError",
    "PromptCacheCoordinatorSnapshot",
    "PromptCachePreparedRequest",
    "PromptCacheResponseObservation",
    "MemoryDeltaPublisher",
    "MemoryDeltaTooLarge",
    "MemoryPublicationSnapshot",
    "PromptCacheDiagnostics",
    "PromptCacheDiagnosticsSnapshot",
    "PromptLayout",
    "fingerprint_json",
    "fingerprint_request",
    "fingerprint_text",
]
