"""PatchLoop public package."""

from patchloop.cache import CacheDiagnostics, CacheLayoutReason, CacheLayoutTrace
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
    "CacheDiagnostics",
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
