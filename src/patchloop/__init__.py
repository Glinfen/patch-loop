"""PatchLoop public package."""

from patchloop.cache import CacheDiagnostics, CacheLayoutReason, CacheLayoutTrace
from patchloop.cache_epoch import (
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

__all__ = [
    "AgentStep",
    "CacheCompressionRequest",
    "CacheDiagnostics",
    "CacheEpoch",
    "CacheEpochBoundary",
    "CacheEpochSnapshot",
    "CacheLayoutReason",
    "CacheLayoutTrace",
    "Plan",
    "PlanItem",
    "PromptCacheLayout",
    "Task",
    "TaskReport",
    "ToolCall",
    "ToolResult",
]
__version__ = "0.1.0"
