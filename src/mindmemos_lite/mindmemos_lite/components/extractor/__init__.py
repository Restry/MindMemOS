"""Public extraction components used by the migrated vanilla pipeline."""

from .protocols import AddRecallStrategy, MemoryExtractor
from .vanilla import (
    AddSafetyGate,
    CandidateDeduplicator,
    ExtractedEntityCandidate,
    ExtractedMemoryCandidate,
    ExtractedSourceCandidate,
    MemoryExtractionResult,
    PlannedAddAction,
    PropertyBinding,
    VanillaMemoryExtractor,
    parse_memory_extraction_json,
)

__all__ = [
    "AddRecallStrategy",
    "AddSafetyGate",
    "CandidateDeduplicator",
    "ExtractedEntityCandidate",
    "ExtractedMemoryCandidate",
    "ExtractedSourceCandidate",
    "MemoryExtractionResult",
    "MemoryExtractor",
    "PlannedAddAction",
    "PropertyBinding",
    "VanillaMemoryExtractor",
    "parse_memory_extraction_json",
]
