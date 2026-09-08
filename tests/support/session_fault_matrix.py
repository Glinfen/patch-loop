"""Compatibility imports for the SRF-07 fault-matrix test helpers."""

from patchloop.evaluation.fault_matrix import (
    EventIntegrity,
    ExpectedState,
    FaultArea,
    FaultBackend,
    FaultMatrix,
    FaultMatrixCase,
    RecoveryResult,
    load_fault_matrix,
)

__all__ = [
    "EventIntegrity",
    "ExpectedState",
    "FaultArea",
    "FaultBackend",
    "FaultMatrix",
    "FaultMatrixCase",
    "RecoveryResult",
    "load_fault_matrix",
]
