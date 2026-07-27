"""Backend-neutral system tables used by :class:`VectorDBService` graphs.

These tables describe the portable graph representation, not a persistence
version or a business row model.  A backend stores them through the same
record/document primitives as every other logical table.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import FieldSpec, FieldType, IndexSpec, TableSpec
from .registry import TableRegistry


@dataclass(frozen=True, slots=True, kw_only=True)
class GraphTableNames:
    """Stable logical names for the service-owned graph tables."""

    node_table: str = "graph_node"
    edge_table: str = "graph_edge"

    def __post_init__(self) -> None:
        if not self.node_table or not self.edge_table:
            raise ValueError("graph table names must not be empty")
        if self.node_table == self.edge_table:
            raise ValueError("graph node and edge tables must have different names")


def build_graph_registry(*, table_names: GraphTableNames | None = None) -> TableRegistry:
    """Build the service-owned graph table definitions.

    The schema is intentionally expressed with generic graph fields.  It does
    not import or validate any ``persistence.v1``/``persistence.v2`` row type.
    """

    names = table_names or GraphTableNames()
    node_fields = (
        FieldSpec(name="node_id", field_type=FieldType.TEXT, nullable=False),
        FieldSpec(name="node_type", field_type=FieldType.TEXT, nullable=False),
    )
    edge_fields = (
        FieldSpec(name="source_id", field_type=FieldType.TEXT, nullable=False),
        FieldSpec(name="source_type", field_type=FieldType.TEXT, nullable=False),
        FieldSpec(name="target_id", field_type=FieldType.TEXT, nullable=False),
        FieldSpec(name="target_type", field_type=FieldType.TEXT, nullable=False),
        FieldSpec(name="relation", field_type=FieldType.TEXT, nullable=False),
        FieldSpec(name="edge_key", field_type=FieldType.JSON, nullable=False),
        FieldSpec(name="properties", field_type=FieldType.JSON, nullable=False),
    )
    registry = TableRegistry(
        (
            TableSpec(
                name=names.node_table,
                primary_key="graph_node_id",
                fields=node_fields,
                indexes=(
                    IndexSpec(
                        name=f"{names.node_table}_identity_uq",
                        fields=("node_type", "node_id"),
                        unique=True,
                    ),
                ),
            ),
            TableSpec(
                name=names.edge_table,
                primary_key="edge_id",
                fields=edge_fields,
                indexes=(
                    IndexSpec(
                        name=f"{names.edge_table}_source_idx",
                        fields=("source_type", "source_id", "relation"),
                    ),
                    IndexSpec(
                        name=f"{names.edge_table}_target_idx",
                        fields=("target_type", "target_id", "relation"),
                    ),
                ),
            ),
        )
    )
    registry.freeze()
    return registry


def with_graph_tables(
    tables: TableRegistry,
    *,
    table_names: GraphTableNames | None = None,
) -> TableRegistry:
    """Return ``tables`` plus the service-owned graph tables.

    Existing same-name tables are accepted only when their definitions are
    exactly equal.  This makes accidental v1/v2 graph schema duplication fail
    at bootstrap instead of silently giving the graph service a different
    representation.
    """

    result = TableRegistry(tables.specs)
    for graph_spec in build_graph_registry(table_names=table_names).specs:
        try:
            existing = result.get(graph_spec.name)
        except KeyError:
            result.register(graph_spec)
        else:
            if existing != graph_spec:
                raise ValueError(f"logical graph table {graph_spec.name!r} has a conflicting definition")
    result.freeze()
    return result


__all__ = ["GraphTableNames", "build_graph_registry", "with_graph_tables"]
