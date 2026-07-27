"""Composition root for all MindMemOS persistence-v2 business tables."""

from __future__ import annotations

from ...infra.vector_store import TableRegistry
from .base import TableDefinition, build_registry
from .memory import memory_table_definitions
from .recorder import recorder_table_definitions
from .skill import skill_table_definitions


def table_definitions(
    *,
    vector_dimensions: int,
    sparse_hash_dim: int,
) -> tuple[TableDefinition, ...]:
    """Return all v2 business tables, grouped behind the three persistence ports."""

    return (
        *memory_table_definitions(
            vector_dimensions=vector_dimensions,
            sparse_hash_dim=sparse_hash_dim,
        ),
        *recorder_table_definitions(),
        *skill_table_definitions(),
    )


def build_v2_registry(
    *,
    vector_dimensions: int,
    sparse_hash_dim: int,
) -> TableRegistry:
    """Build the frozen backend registry for every persistence-v2 business table."""

    return build_registry(
        table_definitions(
            vector_dimensions=vector_dimensions,
            sparse_hash_dim=sparse_hash_dim,
        )
    )


__all__ = ["build_v2_registry", "table_definitions"]
