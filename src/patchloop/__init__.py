"""PatchLoop public package."""

from patchloop.prompt_cache import CacheDiagnostics, CacheLayoutReason, CacheLayoutTrace
from patchloop.prompt_cache import (
    CacheCompressionRequest,
    CacheEpoch,
    CacheEpochBoundary,
    CacheEpochSnapshot,
)
from patchloop.domain import (
    AgentStep,
    Plan,
    PlanItem,
    PromptCacheLayout,
    Task,
    TaskReport,
    ToolCall,
    ToolResult,
)
from patchloop.prompt_cache import (
    MemoryDeltaPublisher,
    MemoryDeltaTooLarge,
    MemoryPublicationSnapshot,
)

__all__ = [
    "AgentStep",
    "CacheCompressionRequest",
    "CacheDiagnostics",
    "CacheEpoch",
    "CacheEpochBoundary",
    "CacheEpochSnapshot",
    "CacheLayoutReason",
    "CacheLayoutTrace",
    "MemoryDeltaPublisher",
    "MemoryDeltaTooLarge",
    "MemoryPublicationSnapshot",
    "Plan",
    "PlanItem",
    "PromptCacheLayout",
    "Task",
    "TaskReport",
    "ToolCall",
    "ToolResult",
]
__version__ = "0.1.0"
