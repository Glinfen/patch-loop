"""Compatibility exports for :mod:`patchloop.prompt_cache.diagnostics`.

Use ``patchloop.prompt_cache`` or its explicit submodules for new imports.
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

__all__ = [
    "CacheDiagnostics",
    "CacheDiagnosticsSnapshot",
    "CacheLayoutReason",
    "CacheLayoutTrace",
    "CacheRequestFingerprint",
    "CacheSectionFingerprint",
    "PromptCacheDiagnostics",
    "PromptCacheDiagnosticsSnapshot",
    "fingerprint_json",
    "fingerprint_request",
    "fingerprint_text",
]
