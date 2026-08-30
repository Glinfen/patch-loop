"""Public contracts for PatchLoop layered memory."""

from patchloop.memory.models import (
    MEMORY_SCHEMA_VERSION,
    CompressionOperation,
    CompressionReport,
    MemoryBundle,
    MemoryHit,
    MemoryKind,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemorySource,
    MemorySourceKind,
    MemoryStatus,
    compute_memory_content_hash,
    validate_supersession_chain,
)
from patchloop.memory.store import (
    MEMORY_STORE_SCHEMA_VERSION,
    MemoryStoreConflictError,
    MemoryStoreError,
    MemoryVectorIndex,
    MemoryWriteResult,
    SQLiteMemoryStore,
)

__all__ = [
    "MEMORY_SCHEMA_VERSION",
    "MEMORY_STORE_SCHEMA_VERSION",
    "CompressionOperation",
    "CompressionReport",
    "MemoryBundle",
    "MemoryHit",
    "MemoryKind",
    "MemoryQuery",
    "MemoryRecord",
    "MemoryScope",
    "MemorySource",
    "MemorySourceKind",
    "MemoryStatus",
    "MemoryStoreConflictError",
    "MemoryStoreError",
    "MemoryVectorIndex",
    "MemoryWriteResult",
    "SQLiteMemoryStore",
    "compute_memory_content_hash",
    "validate_supersession_chain",
]
