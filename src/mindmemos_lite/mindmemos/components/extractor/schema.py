"""Compatibility helpers shared by copied schema-aware algorithms."""

from ...typing import MemoryView, MemoryWrite


def memory_embedding_text(memory: MemoryWrite | MemoryView) -> str:
    """Build the canonical property-memory indexing text."""

    entity_name = str((memory.metadata or {}).get("entity_name") or "")
    property_name = str(memory.property_name or "")
    return f"{entity_name}:{property_name}:{memory.content}"


__all__ = ["memory_embedding_text"]
