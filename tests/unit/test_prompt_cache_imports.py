from patchloop import CacheDiagnostics as root_diagnostics
from patchloop.cache import (
    CacheDiagnostics as legacy_diagnostics,
)
from patchloop.cache import (
    CacheLayoutTrace as legacy_trace,
)
from patchloop.cache import (
    fingerprint_request as legacy_fingerprint_request,
)
from patchloop.cache_epoch import CacheEpoch as legacy_epoch
from patchloop.memory_publication import MemoryDeltaPublisher as legacy_publisher
from patchloop.prompt_cache import (
    CacheDiagnostics,
    CacheEpoch,
    CacheLayoutTrace,
    MemoryDeltaPublisher,
    diagnostics,
    epoch,
    fingerprint_request,
    layout,
    publication,
)
from patchloop.prompt_cache.layout import PromptLayout
from patchloop.prompt_layout import PromptLayout as legacy_layout


def test_legacy_modules_are_identity_preserving_reexports() -> None:
    assert legacy_diagnostics is CacheDiagnostics is root_diagnostics
    assert legacy_trace is CacheLayoutTrace
    assert legacy_fingerprint_request is fingerprint_request
    assert legacy_epoch is CacheEpoch
    assert legacy_layout is PromptLayout is layout.PromptLayout
    assert legacy_publisher is MemoryDeltaPublisher


def test_prompt_cache_package_exports_only_stable_public_names() -> None:
    assert "_canonical_json" not in diagnostics.__all__
    assert "_normalize_summary" not in epoch.__all__
    assert "_payload_delta" not in publication.__all__
